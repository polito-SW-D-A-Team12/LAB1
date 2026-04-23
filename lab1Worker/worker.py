import os
import time
import html
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from pymongo import MongoClient, ReturnDocument
from bson import ObjectId


load_dotenv()


MONGODB_URI = os.getenv("MONGODB_URI")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "5"))
SMTP_HOST = os.getenv("SMTP_HOST", "localhost")
SMTP_PORT = int(os.getenv("SMTP_PORT", "1025"))
EMAIL_FROM = os.getenv("EMAIL_FROM", "worker@mzinga.io")


if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI is not set")


client = MongoClient(MONGODB_URI)
db = client.get_database("mzinga")

communications = db["communications"]
users = db["users"]


def get_relation_object_id(reference: Dict[str, Any]) -> Optional[ObjectId]:
    value = reference.get("value")

    if isinstance(value, ObjectId):
        return value

    if isinstance(value, str):
        return ObjectId(value)

    if isinstance(value, dict):
        raw_id = value.get("_id") or value.get("id")
        if raw_id:
            return ObjectId(raw_id)

    return None


def resolve_user_emails(references: Optional[List[Dict[str, Any]]]) -> List[str]:
    if not references:
        return []

    object_ids = []

    for reference in references:
        relation_to = reference.get("relationTo")
        if relation_to != "users":
            continue

        object_id = get_relation_object_id(reference)
        if object_id:
            object_ids.append(object_id)

    if not object_ids:
        return []

    found_users = users.find(
        {"_id": {"$in": object_ids}},
        {"email": 1},
    )

    emails = []

    for user in found_users:
        email = user.get("email")
        if email:
            emails.append(email)

    return emails


def serialize_leaf(node: Dict[str, Any]) -> str:
    text = html.escape(node.get("text", ""))

    if node.get("bold"):
        text = f"<strong>{text}</strong>"

    if node.get("italic"):
        text = f"<em>{text}</em>"

    return text


def serialize_children(node: Dict[str, Any]) -> str:
    children = node.get("children", [])
    return "".join(serialize_node(child) for child in children)


def serialize_node(node: Dict[str, Any]) -> str:
    if "text" in node:
        return serialize_leaf(node)

    node_type = node.get("type")
    children_html = serialize_children(node)

    if node_type in (None, "paragraph"):
        return f"<p>{children_html}</p>"

    if node_type == "h1":
        return f"<h1>{children_html}</h1>"

    if node_type == "h2":
        return f"<h2>{children_html}</h2>"

    if node_type == "ul":
        return f"<ul>{children_html}</ul>"

    if node_type == "li":
        return f"<li>{children_html}</li>"

    if node_type == "link":
        url = html.escape(node.get("url", "#"))
        return f'<a href="{url}">{children_html}</a>'

    return children_html


def serialize_body_to_html(body: Any) -> str:
    if not body:
        return ""

    if isinstance(body, list):
        return "".join(serialize_node(node) for node in body)

    if isinstance(body, dict):
        return serialize_node(body)

    return html.escape(str(body))


def send_email(
    subject: str,
    to_emails: List[str],
    cc_emails: List[str],
    bcc_emails: List[str],
    html_body: str,
) -> None:
    if not to_emails:
        raise RuntimeError("No valid recipient emails found in tos")

    message = MIMEMultipart("alternative")
    message["From"] = EMAIL_FROM
    message["To"] = ", ".join(to_emails)
    message["Subject"] = subject or ""

    if cc_emails:
        message["Cc"] = ", ".join(cc_emails)

    recipients = to_emails + cc_emails + bcc_emails

    message.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.sendmail(EMAIL_FROM, recipients, message.as_string())


def claim_pending_communication() -> Optional[Dict[str, Any]]:
    return communications.find_one_and_update(
        {"status": "pending"},
        {"$set": {"status": "processing"}},
        return_document=ReturnDocument.AFTER,
    )


def process_communication(document: Dict[str, Any]) -> None:
    document_id = document["_id"]

    try:
        print(f"[worker] Processing communication {document_id}")

        subject = document.get("subject", "")
        body = document.get("body", [])

        to_emails = resolve_user_emails(document.get("tos"))
        cc_emails = resolve_user_emails(document.get("ccs"))
        bcc_emails = resolve_user_emails(document.get("bccs"))

        html_body = serialize_body_to_html(body)

        send_email(
            subject=subject,
            to_emails=to_emails,
            cc_emails=cc_emails,
            bcc_emails=bcc_emails,
            html_body=html_body,
        )

        communications.update_one(
            {"_id": document_id},
            {"$set": {"status": "sent"}},
        )

        print(f"[worker] Communication {document_id} marked as sent")

    except Exception as error:
        communications.update_one(
            {"_id": document_id},
            {"$set": {"status": "failed"}},
        )

        print(f"[worker] Communication {document_id} failed: {error}")


def main() -> None:
    print("[worker] Started")
    print(f"[worker] Poll interval: {POLL_INTERVAL_SECONDS}s")
    print(f"[worker] SMTP: {SMTP_HOST}:{SMTP_PORT}")

    while True:
        document = claim_pending_communication()

        if not document:
            print("[worker] No pending communications")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        process_communication(document)


if __name__ == "__main__":
    main()