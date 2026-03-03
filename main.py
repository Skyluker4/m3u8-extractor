import time
import re
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
import yt_dlp
from urllib.parse import urljoin


def fetch_m3u8_and_download(url):
    # Configure WebDriver options
    chrome_options = Options()
    chrome_options.add_argument(
        "--headless"
    )  # Run in headless mode (without opening a browser window)

    # Setup WebDriver (make sure ChromeDriver is in your PATH)
    driver = webdriver.Chrome(options=chrome_options)

    try:
        # Open the URL with the WebDriver
        driver.get(url)

        # Allow the page to load and execute any JS
        time.sleep(5)  # Adjust time if necessary depending on page complexity

        # Get the page title
        page_title = driver.title.strip()

        # Fetch the page source and extract the m3u8 URL (using regex)
        page_source = driver.page_source

        # Regular expression to find m3u8 URLs
        m3u8_url = None
        m3u8_pattern = r'(https?://[^\s"]+\.m3u8)'
        match = re.search(m3u8_pattern, page_source)

        if match:
            m3u8_url = match.group(0)

        # If the URL isn't found or it's incomplete (starts with "t:")
        if not m3u8_url or m3u8_url.startswith("t:"):
            print("Fixing the m3u8 URL...")

            # Ensure we prepend the base URL if the extracted m3u8 URL is relative
            base_url = url  # Use the page URL as the base for relative URLs
            m3u8_url = urljoin(
                base_url, m3u8_url
            )  # Join the base URL and the relative m3u8 URL

            # If the URL still starts with "t:", prepend the source base URL
            if m3u8_url.startswith("t:"):
                m3u8_url = (
                    url + m3u8_url[2:]
                )

        if not m3u8_url:
            print("m3u8 URL not found in the page source.")
            return

        # Close the WebDriver
        driver.quit()

        # Download the video using yt-dlp
        if m3u8_url:
            print(f"Found m3u8 URL: {m3u8_url}")
            print(f"Downloading {page_title}...")

            ydl_opts = {
                "outtmpl": f"{page_title}.%(ext)s",  # Use the page title for the filename
                "quiet": False,
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([m3u8_url])
            print(f"Download completed: {page_title}")
        else:
            print("No valid m3u8 URL found.")
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        driver.quit()


def download_from_file(file_path):
    try:
        with open(file_path, "r") as file:
            urls = file.readlines()

        # Clean URLs (remove empty lines and strip whitespace)
        urls = [url.strip() for url in urls if url.strip()]

        if not urls:
            print("No URLs found in the file.")
            return

        for url in urls:
            fetch_m3u8_and_download(url)

    except FileNotFoundError:
        print(f"Error: File '{file_path}' not found.")
    except Exception as e:
        print(f"An error occurred while reading the file: {e}")


# Path to the file containing URLs (one per line)
file_path = "urls.txt"

download_from_file(file_path)
