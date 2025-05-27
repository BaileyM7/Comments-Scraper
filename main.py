# Required: pip install requests openai pdfplumber

import os
import requests
import pdfplumber
from datetime import datetime, date
from openai import OpenAI

# === CONFIG ===
with open("keys/openai.txt") as f:
    OPENAI_API_KEY = f.read().strip()

with open("keys/regulation.txt") as f:
    API_KEY = f.read().strip()

print("OPENAI_API_KEY loaded:", bool(OPENAI_API_KEY))
print("REGULATIONS_API_KEY loaded:", bool(API_KEY))

BASE_URL = "https://api.regulations.gov/v4/comments"
MAX_RESULTS = 10  # Adjust for more

# === OpenAI Client ===
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# === Utility to determine if text is from an organization ===
def is_from_organization(text):
    prompt = (
        "Classify the following comment as written by either an individual or an organization. "
        "Use the number 1 if it was written by an organization, and 2 if it was written by an individual. "
        "An organization includes companies, associations, coalitions, nonprofits, universities, or any group "
        "that speaks on behalf of members. Use the content of the message—not just the name or signature—for your decision."
    )

    response = openai_client.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text[:3000]}
        ]
    )

    msg = response.choices[0].message.content.strip()
    print("\n--- GPT Response for Org Check ---")
    print(msg)
    print("----------------------------------")
    return "1" in msg

# === Utility to summarize text ===
def summarize_text(text):
    response = openai_client.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "Summarize this organizational public comment:"},
            {"role": "user", "content": text[:8000]}  # truncate if needed
        ]
    )
    return response.choices[0].message.content

# === Utility to download and extract text from PDF ===
def extract_pdf_text(url):
    response = requests.get(url)
    with open("temp.pdf", "wb") as f:
        f.write(response.content)
    with pdfplumber.open("temp.pdf") as pdf:
        text = "\n".join([page.extract_text() for page in pdf.pages if page.extract_text()])
    os.remove("temp.pdf")
    print("\n--- Extracted PDF Text Preview ---")
    print(text[:1000])  # print the first 1000 characters for debugging
    print("----------------------------------")
    return text

# === Retrieve full comment info (with fallback for attachments) ===
def fetch_comment_detail(comment_id):
    detail_url = f"{BASE_URL}/{comment_id}?include=attachments&api_key={API_KEY}"
    print(f"Fetching full comment detail: {detail_url}")
    response = requests.get(detail_url)
    if response.status_code == 200:
        return response.json()
    else:
        print("Failed to retrieve detail for comment.", response.status_code)
        return None

# === Main Process ===
def fetch_comments_with_attachments():
    today = date.today().isoformat()
    url = f"{BASE_URL}?filter[postedDate]={today}&include=attachments&page[size]={MAX_RESULTS}&api_key={API_KEY}"
    response = requests.get(url)

    print("API Status Code:", response.status_code)
    print("Response JSON keys:", response.json().keys())

    data = response.json()
    comments = data.get("data", [])
    included = data.get("included", [])
    attachments = {item["id"]: item for item in included if item["type"] == "attachments"}

    print(f"Total comments retrieved: {len(comments)}")
    print(f"Total attachments included in payload: {len(attachments)}")
    print("Attachment IDs from 'included':", list(attachments.keys()))

    for comment in comments:
        comment_title = comment.get("attributes", {}).get("title", "<No Title>")
        comment_id = comment.get("id")
        print(f"\n--- Comment Title: {comment_title} ---")

        # Fallback: re-fetch full comment details for attachment check
        detail_data = fetch_comment_detail(comment_id)
        if not detail_data:
            continue

        comment_data = detail_data.get("data", {})
        included_data = detail_data.get("included", [])
        detail_attachments = {item["id"]: item for item in included_data if item["type"] == "attachments"}

        comment_attachments = comment_data.get("relationships", {}).get("attachments", {}).get("data", [])
        print(f"Found {len(comment_attachments)} attachments for comment.")
        if not comment_attachments:
            print("(No relationships -> attachments data block found)")
            continue

        for att in comment_attachments:
            att_id = att.get("id")
            att_info = detail_attachments.get(att_id)
            if att_info:
                file_formats = att_info.get("attributes", {}).get("fileFormats", [])
                file_url = None
                for fmt in file_formats:
                    if "fileUrl" in fmt:
                        file_url = fmt["fileUrl"]
                        break

                print("Available file formats:", file_formats)
                print("Selected file URL:", file_url)

                if not file_url:
                    print("Skipped: No valid fileUrl found.")
                    continue

                print(f"Downloading attachment: {file_url}")
                text = extract_pdf_text(file_url)
                print(f"Extracted text length: {len(text)}")
                if not text.strip():
                    print("Skipped: No text extracted from PDF.")
                    continue

                # Heuristic check on the comment title
                org_keywords = ["llc", "inc", "organization", "institute", "university", "center", "society", "association", "coalition", "agency", "company", "group", "corporation"]
                title_lower = comment_title.lower()
                title_has_org = any(word in title_lower for word in org_keywords)

                if title_has_org:
                    print("Detected organization based on title.")
                    is_org = True
                else:
                    print("Title did not clearly indicate an organization. Asking GPT...")
                    is_org = is_from_organization(text)

                print(f"GPT/Heuristic determined organization? {is_org}")

                if is_org:
                    summary = summarize_text(text)
                    print("Summary:\n", summary)
                else:
                    print("Skipped: Not from organization")
                break  # only handle one attachment per comment for now

if __name__ == "__main__":
    fetch_comments_with_attachments()
