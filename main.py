import os
import csv
import requests
import pdfplumber
import pytesseract
from pdf2image import convert_from_path
from datetime import datetime, timedelta
import platform
from openai import OpenAI
from cleanup_text import clean_text

# === CONFIG ===
with open("keys/openai.txt") as f:
    OPENAI_API_KEY = f.read().strip()

with open("keys/regulation.txt") as f:
    API_KEY = f.read().strip()

BASE_URL = "https://api.regulations.gov/v4/comments"
MAX_RESULTS = 200

openai_client = OpenAI(api_key=OPENAI_API_KEY)

# === Utility: Write CSV output ===
def write_to_csv(records, output_file="test.csv"):
    with open(output_file, mode="w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=["filename", "headline", "body"])
        writer.writeheader()
        for record in records:
            writer.writerow(record)

# === Utility: Determine if the text is from a valid organization letter ===
def is_from_organization(text):
    prompt = (
        "Classify the following public comment as valid if it meets BOTH conditions:\n"
        "1. It is written by an organization (e.g., company, nonprofit, university, agency, coalition).\n"
        "2. It takes the form of a formal letter or memo directed TO someone — such as a federal agency or official — "
        "usually indicated by a salutation, heading, or opening lines that name the recipient.\n\n"
        "Use the number 1 if BOTH conditions are met.\n"
        "Use the number 2 if either condition is missing or unclear.\n\n"
        "Text:\n"
    )
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text[:3000]}
        ]
    )
    msg = response.choices[0].message.content.strip()
    return "1" in msg

# === Utility: Extract author name, avoiding middle initials ===
def extract_author_name(text, last_page_text=""):
    prompt = (
        "You are analyzing the end of a public comment letter. Extract the name of the person who signed it.\n"
        "Look near the end of the document for sign-offs like 'Sincerely', 'Respectfully', 'From:' or blocks with a name below a title.\n"
        "Ignore any middle names or initials, credentials, or titles.\n"
        "Return just the first and last name together as a single word with no spaces (e.g., MichelleOwen).\n"
        "If no clear name appears, return 'Unknown'.\n\n"
        "Text:\n"
    )
    
    user_input = last_page_text if last_page_text.strip() else text[-4000:]
    # print(user_input)
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_input}
        ]
    )
    name = response.choices[0].message.content.strip()
    parts = name.split()
    if len(parts) >= 2:
        return parts[0] + parts[-1]
    return "AUTHNOTFOUND" if name.lower() == "unknown" else name.replace(" ", "")

# === Summarize the comment content into a news story ===
def summarize_text(text, agency_name, comment_title):
    prompt = (
        f"Create a 300-word news story with a headline based on the following letter to the federal agency named '{agency_name}'.\n\n"
        "In the headline, always spell out the full name of the agency — do not use acronyms.\n\n"
        "For documents from government, college, or public policy groups (including coalitions and alliances): "
        "Write a news story with stand-alone paragraphs, incorporating direct quotes from the letter's author. "
        "Avoid using a dateline or acronyms in the body. Be sure to identify the date the letter was written (e.g., 'in a letter dated May 23, 2025').\n\n"
        "For documents from business entities: Write a news story with stand-alone paragraphs, incorporating direct "
        "quotes from the letter's author. Include the city/state of the company filing the comment in the text. "
        "Avoid using a dateline or acronyms in the body. Also include the date the letter was written if available.\n\n"
        f"Also describe the organization(s) involved based on this comment title: {comment_title}"
    )
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text[:8000]}
        ]
    )
    return response.choices[0].message.content

# === Extract text and retain last page separately with OCR fallback
def extract_pdf_text(url):
    response = requests.get(url)
    with open("temp.pdf", "wb") as f:
        f.write(response.content)

    full_text = ""
    last_page_text = ""

    try:
        with pdfplumber.open("temp.pdf") as pdf:
            texts = [page.extract_text() or "" for page in pdf.pages]
            full_text = "\n".join(texts)
            last_page_text = texts[-1] if texts else ""

            if not last_page_text.strip() and pdf.pages:
                last_page_image = convert_from_path("temp.pdf", first_page=len(pdf.pages), last_page=len(pdf.pages))[0]
                last_page_text = pytesseract.image_to_string(last_page_image)
    except Exception as e:
        print("pdfplumber error:", e)

    if not full_text.strip():
        print("Falling back to full OCR...")
        images = convert_from_path("temp.pdf")
        full_text = "\n".join([pytesseract.image_to_string(img) for img in images])

    os.remove("temp.pdf")
    return full_text, last_page_text

# === Fetch comment detail
def fetch_comment_detail(comment_id):
    detail_url = f"{BASE_URL}/{comment_id}?include=attachments&api_key={API_KEY}"
    response = requests.get(detail_url)
    return response.json() if response.status_code == 200 else None

# === Main logic ===
def fetch_comments_with_attachments():
    today = datetime.today()
    yesterday = today - timedelta(days=1)
    is_windows = platform.system() == "Windows"

    if is_windows:
        today_date_md = f"{today.strftime('%B')} {today.day}"
        yesterday_date_mdy = f"{yesterday.strftime('%B')} {yesterday.day}, {yesterday.year}"
    else:
        today_date_md = today.strftime("%B %-d")
        yesterday_date_mdy = yesterday.strftime("%B %-d, %Y")

    date_filter = yesterday.strftime("%Y-%m-%d")
    url = f"{BASE_URL}?filter[postedDate]={date_filter}&include=attachments&page[size]={MAX_RESULTS}&api_key={API_KEY}"
    response = requests.get(url)
    print("API Status Code:", response.status_code)

    data = response.json()
    comments = data.get("data", [])
    included = data.get("included", [])
    attachments = {item["id"]: item for item in included if item["type"] == "attachments"}

    total_checked = 0
    valid_outputs = 0
    output_records = []

    for comment in comments:
        total_checked += 1
        comment_title = comment.get("attributes", {}).get("title", "<No Title>")
        agency = comment.get("attributes", {}).get("agency", "a federal agency")
        agency_id = comment.get("attributes", {}).get("agencyId", "UNKNOWN")
        comment_id = comment.get("id")

        detail_data = fetch_comment_detail(comment_id)
        if not detail_data:
            continue

        comment_data = detail_data.get("data", {})
        included_data = detail_data.get("included", [])
        detail_attachments = {item["id"]: item for item in included_data if item["type"] == "attachments"}
        comment_attachments = comment_data.get("relationships", {}).get("attachments", {}).get("data", [])
        if not comment_attachments:
            continue

        for att in comment_attachments:
            att_id = att.get("id")
            att_info = detail_attachments.get(att_id)
            if att_info:
                file_formats = att_info.get("attributes", {}).get("fileFormats", [])
                try:
                    file_url = next((fmt["fileUrl"] for fmt in file_formats if isinstance(fmt, dict) and "fileUrl" in fmt), None)
                except Exception as e:
                    print(f"[Error extracting fileUrl] Comment ID: https://www.regulations.gov/comment/{comment_id}")                  
                    continue
                if not file_url:
                    continue

                text, last_page_text = extract_pdf_text(file_url)
                if not text.strip():
                    continue

                if not is_from_organization(text):
                    continue

                summary = summarize_text(text, agency, comment_title)
                lines = summary.strip().split("\n", 1)
                headline = lines[0].strip()
                body_main = lines[1].strip() if len(lines) > 1 else ""

                body = (
                    f"WASHINGTON, {today_date_md} -- {body_main}\n\n* * *\n\n"
                    f"Read full text of the public communication here: {file_url}\n\n"
                    f"View Regulations.gov posting on {yesterday_date_mdy} and docket information here: "
                    f"https://www.regulations.gov/comment/{comment_id}."
                )

                year_short = str(today.year)[2:]
                month = f"{today.month:02d}"
                day = str(today.day)
                author = extract_author_name(text, last_page_text)
                filename = f"$H {year_short}{month}{day}--PubCom-{agency_id}-{author}"
                filename = clean_text(filename)

                output_records.append({
                    "filename": filename,
                    "headline": clean_text(headline),
                    "body": clean_text(body)
                })
                valid_outputs += 1
                break

    write_to_csv(output_records)
    print(f"\nTotal comments checked: {total_checked}")
    print(f"Valid organization summaries generated: {valid_outputs}")

if __name__ == "__main__":
    fetch_comments_with_attachments()