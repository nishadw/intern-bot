import os
import json
import smtplib
import ssl
import re
from threading import Thread
from time import perf_counter, time, localtime, strftime
from email.mime.text import MIMEText

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# --- Configuration ---

# Scraping
HEIGHT = 32
MAX_ITERATIONS = 75  # Failsafe if stop_rowid = []
MAX_SEEN_ITEMS = 500 # Max items to store per link in seen_items.json
WHITELIST_SIZES = ('1001-5000', '5001-10000', '10000+')

# Formatting
GAP = 2
DELIM = " " * GAP + "|" + " " * GAP
RECIPIENT_SPACING = {
    "akshat.wajge@gmail.com": {"title": 60, "company": 25, "date": 10, "location": 20, "tags": 40},
    "nishad.wajge@gmail.com": {"title": 85, "company": 35, "date": 10, "location": 20, "tags": 55}
}

# Email
PORT = 465
SMTP_SERVER = "smtp.gmail.com"
USERNAME = os.environ.get('USER_EMAIL')
PASSWORD = os.environ.get('USER_PASSWORD')
RECIPIENTS = os.environ.get('RECIPIENTS', "").split(",")

# Selenium Options
options = Options()
options.add_argument("--headless=new")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")

# --- Global State ---
# This is modified by threads
internships = {}  # {link: {"category": name, "links": [...]}}
start_time = perf_counter()

# --- Helper Functions ---

def load_json(filename, default_value):
    """Safely loads a JSON file, returning a default value on failure."""
    try:
        with open(filename, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default_value

def get_innertext(driver, row, category, div_class="truncate", multiple=False):
    """Extracts innerText from a cell based on the category (column header)."""
    col_index = find_columnindex(driver, category)
    selector = f'div[data-columnindex="{col_index}"] div.{div_class}'
    
    matches = [
        match.get_attribute("innerText")
        for match in row.find_element(By.CSS_SELECTOR, f'div[data-columnindex="{col_index}"]')
                    .find_elements(By.CSS_SELECTOR, f"div.{div_class}")
    ]

    return matches if multiple else (matches[0] if matches else None)

def find_columnindex(driver, category):
    """Finds the dynamic column index for a given category name."""
    header = driver.find_element(By.XPATH, f'//div[text()="{category}"]')
    # Traverse up to the parent container that has the data-columnindex
    return header.find_element(By.XPATH, "../../../../../..").get_attribute("data-columnindex")

def append_data(driver, row):
    """Builds a dictionary of internship data from a single row element."""
    row_id = row.get_attribute("data-rowid")
    
    # Title is in the left pane, not the main row context
    title_row = driver.find_element(By.CSS_SELECTOR, f'div[data-rowid="{row_id}"]')
    title = get_innertext(driver, title_row, "Position Title")
    
    company = get_innertext(driver, row, "Company")
    date = get_innertext(driver, row, "Date")
    location = get_innertext(driver, row, "Location")
    tags = get_innertext(driver, row, "Company Industry", "flex-auto.truncate-pre", True)
    
    # Find the parent 'a' tag to get the href
    apply_link_element = row.find_element(By.CSS_SELECTOR, "span.truncate.noevents")
    apply_link = apply_link_element.find_element(By.XPATH, "..").get_attribute("href")

    if "Multi Location" in location:
        location = "Multi Location"
    if not tags:
        tags = ["None"]

    return {"title": title, "company": company, "date": date, "location": location, "tags": tags, "apply_link": apply_link}

def add_internships(link, seen_links_set, attempts=1):
    """Scrapes a single internship link. Designed to be run in a thread."""
    
    # 'seen_links_set' is now passed in as an argument

    driver = webdriver.Chrome(options=options)
    driver.set_window_size(1920, 1080)  # Necessary for some elements to render
    driver.set_page_load_timeout(10)
    wait = WebDriverWait(driver, 10)

    try:
        driver.get(link)
        wait.until(EC.presence_of_element_located((By.ID, "airtable-box")))
        list_name = driver.find_element(By.CSS_SELECTOR, "h2.active").get_attribute("innerText")
        
        airtable_url = driver.find_element(By.ID, "airtable-box").get_attribute("src")
        driver.get(airtable_url)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.dataRow.rightPane")))

    except Exception:
        # Retry the entire function on navigation failure
        driver.close()
        add_internships(link, seen_links_set, attempts + 1) # Pass the set in the retry
        return

    scrollable = driver.find_element(By.CSS_SELECTOR, "div.antiscroll-inner")
    
    local_dict = {}
    finished = False
    row_count = 0
    
    while not finished:
        elements = driver.find_elements(By.CSS_SELECTOR, "div.dataRow.rightPane.rowExpansionEnabled.rowSelectionEnabled")
        
        if row_count < len(elements) - 1:
            row = elements[row_count]
        else:
            # Load new row at the bottom and scroll
            row = elements[-1]
            driver.execute_script(f"arguments[0].scrollTop += {HEIGHT};", scrollable)

        row_data = append_data(driver, row)

        # Stop if the link is IN THE SEEN DATABASE, or we hit the failsafe
        if (row_data["apply_link"] in seen_links_set) or (len(local_dict) == MAX_ITERATIONS):
            finished = True
        elif get_innertext(driver, row, "Company Size", "flex-auto.truncate-pre") in WHITELIST_SIZES:
            local_dict[row.get_attribute("data-rowid")] = row_data
        
        row_count += 1

    # Update global state (mutations are on unique keys, so thread-safe-ish)
    scraped_links = list(local_dict.values())
    internships[link] = {"category": list_name, "links": scraped_links}

    print(f'Thread "{link}" processed {len(scraped_links)} items in {(perf_counter() - start_time):.3f}s ({attempts} attempt(s))')
    driver.close()

# --- Formatting and Email ---

def truncate(string, num, part=True):
    """Pads or truncates a string to a fixed length."""
    return string.ljust(num)[:num] + (DELIM if part else " " * GAP)

def format_internship_html(data, custom_space, on_watchlist, in_cali):
    """Formats a single internship entry into an HTML line."""
    link_sub = truncate(data["title"], custom_space["title"], False).strip()
    
    line = (f'<a href="{data["apply_link"]}" target="_blank">{link_sub}</a>')
    line += ' ' * (custom_space["title"] - len(link_sub)) + DELIM
    line += truncate(data["company"], custom_space["company"])
    line += truncate(data["date"], custom_space["date"])
    line += truncate(data["location"], custom_space["location"])
    line += truncate(", ".join(str(tag) for tag in data["tags"]), custom_space["tags"], False)

    # Apply highlighting
    if on_watchlist:
        line = f'<span style="background-color: #fff8b3;">{line}</span>'
    elif in_cali:
        line = f'<span style="background-color: #c8f7c5;">{line}</span>'

    return line + "\n"

def make_message_html(recipient, internship_links, watchlist):
    """Builds the full HTML email body for a specific recipient."""
    message_text = ""
    global_watchlist_entries = []
    processed_watchlist_links = set() # To track items already in the global list

    # --- 1. First Pass: Collect all watchlist items ---
    all_links_in_order = internship_links + [k for k in internships if k not in internship_links]
    
    for link in all_links_in_order:
        if link not in internships:
            continue
        
        link_data = internships[link]
        for data in link_data["links"]:
            on_watchlist = data["company"].strip() in watchlist
            if on_watchlist:
                # Format it (in_cali doesn't matter for watchlist highlighting)
                line_html = format_internship_html(data, RECIPIENT_SPACING[recipient], on_watchlist=True, in_cali=False)
                global_watchlist_entries.append(line_html)
                processed_watchlist_links.add(data["apply_link"]) # Mark as processed

    # --- 2. Build the Watchlist section at the top ---
    if global_watchlist_entries:
        message_text += f"===== ⭐ Watchlist ({len(global_watchlist_entries)}) =====\n\n"
        message_text += "".join(global_watchlist_entries)

    # --- 3. Second Pass: Process all other items ---
    first_regular_list = True
    for link in all_links_in_order:
        if link not in internships:
            continue
            
        link_data = internships[link]
        
        instate_entries = []
        regular_entries = []

        for data in link_data["links"]:
            # *** CHECK IF ALREADY PROCESSED ***
            if data["apply_link"] in processed_watchlist_links:
                continue # Skip, it's in the global list

            # Item is not on watchlist, check if it's in CA
            in_cali = any(match in data["location"] for match in ["CA", "California"])
            
            # Pass on_watchlist=False since it's not
            line_html = format_internship_html(data, RECIPIENT_SPACING[recipient], on_watchlist=False, in_cali=in_cali)
            
            if in_cali:
                instate_entries.append(line_html)
            else:
                regular_entries.append(line_html)

        # Combine groups for this link
        text_subsection = "".join(instate_entries) + "".join(regular_entries)
        
        if text_subsection: # Only add header if there are non-watchlist items
            if global_watchlist_entries and first_regular_list:
                 message_text += "\n\n" + ("-" * 40) + "\n" # Add a big separator
                 first_regular_list = False # Only do this once

            category_name = re.sub(r"[^a-zA-Z0-9 ]+", "", link_data["category"]).strip()
            # Get count of *only* the new items for this list
            new_item_count = len(instate_entries) + len(regular_entries) 
            
            header = f'\n===== From: <a href="{link}" target="_blank">{category_name}</a> ({new_item_count}) =====\n\n'
            message_text += header + text_subsection

    # --- 4. Create and return the email message object ---
    total_internships = sum(len(data["links"]) for data in internships.values())
    email_html = f'<pre style="font-family: monospace;">{message_text}</pre>'
    message = MIMEText(email_html, 'html')

    message['Subject'] = f"Intern Bot 🤖 : {total_internships} internships found on {strftime('%m/%d/%Y', localtime(time()))}"
    message["From"] = USERNAME
    message["To"] = recipient
    
    return message.as_string()


def send_emails(internship_links, watchlist):
    """Logs into SMTP server and sends all emails."""
    print("Connecting to email server...")
    context = ssl.create_default_context()
    
    with smtplib.SMTP_SSL(SMTP_SERVER, PORT, context=context) as server:
        server.login(USERNAME, PASSWORD)
        for recipient in [r.strip() for r in RECIPIENTS if r.strip]:
            # Pass watchlist to the message builder
            message_string = make_message_html(recipient, internship_links, watchlist)
            server.sendmail(USERNAME, recipient, message_string)
            print(f"Message sent to {recipient}")
            
# --- Main Execution ---

def main():
    """Main script logic."""
    # 1. Load configuration files
    internship_links = load_json("links.json", [])
    all_seen_data = load_json("seen_items.json", {}) 
    watchlist = load_json("watchlist.json", []) # Watchlist is loaded here

    if not internship_links:
        print("No links found in 'links.json'. Exiting.")
        return
        
    if not USERNAME or not PASSWORD or not RECIPIENTS:
        print("Email credentials (USER_EMAIL, USER_PASSWORD, RECIPIENTS) not set in environment. Exiting.")
        return

    # 2. Start scraping threads
    threads = []
    for link in internship_links:
        seen_links_for_thread = set(all_seen_data.get(link, []))
        t = Thread(target=add_internships, args=(link, seen_links_for_thread))
        threads.append(t)
        t.start()

    # 3. Wait for all threads to complete
    [t.join() for t in threads]
    print("Scraping complete.")

    # 4. Update and save 'seen_items.json'
    new_all_seen_data = {}
    for link_url in internship_links:
        new_links = [item["apply_link"] for item in internships.get(link_url, {}).get("links", [])]
        old_links_set = set(all_seen_data.get(link_url, []))
        old_links_set.update(new_links)
        
        new_list = list(old_links_set)
        if len(new_list) > MAX_SEEN_ITEMS:
            new_list = new_list[-MAX_SEEN_ITEMS:] 
            
        new_all_seen_data[link_url] = new_list

    with open("seen_items.json", "w") as f:
        json.dump(new_all_seen_data, f, indent=4)
        print("Wrote 'seen_items.json'.")

    # 5. Send emails
    if any(data["links"] for data in internships.values()):
        # Pass watchlist to the send_emails function
        send_emails(internship_links, watchlist)
    else:
        print("No new internships found. No emails sent.")

    print(f"Script finished in {(perf_counter() - start_time):.3f} seconds.")

if __name__ == "__main__":
    main()