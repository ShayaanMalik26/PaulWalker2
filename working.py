import logging
from langchain_community.document_loaders import WebBaseLoader
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langchain.output_parsers import PydanticOutputParser
from pydantic import BaseModel, Field
from langchain_core.documents import Document
import json
from dotenv import load_dotenv
import os
from ipfs_handler import IPFSHandler
from typing import Dict, List
import time
import pandas as pd
# from getlinks import getLinks
import re
from bs4 import BeautifulSoup
import requests
import html2text
import asyncio
import chromadb
import uuid
from sentence_transformers import SentenceTransformer
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
# from send_to_api import send_json_to_api  # Comment out or remove this line

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('working.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# Initialize SentenceTransformer model
model = SentenceTransformer("all-MiniLM-L6-v2")

# Define a Pydantic model for structured output
class PatentInfo(BaseModel):
    inventions: List[str] = Field(description="List of inventions claimed in the patent")

# Load environment variables at the top of the file
load_dotenv()

class PatentScraper:
    def __init__(self):
        # ... existing initialization ...
        self.ipfs_handler = IPFSHandler()

    def save_to_ipfs(self, patent_data: Dict) -> Dict:
        """
        Format and save patent data to IPFS
        """
        formatted_data = {
                "patent_title": patent_data.get('patent_title', ''),
                "abstract": patent_data.get('abstract', ''),
                "inventions": patent_data.get('inventions', []),
                "patent_number": patent_data.get('publication_number', ''),
                "filing_date": patent_data.get('filing_date', ''),
                "assignee_name": patent_data.get('assignee_name', ''),
                "inventor_name": patent_data.get('inventor_name', ''),
                "patent_url": patent_data.get('patent_url', ''),
                "patent_text": patent_data.get('patent_text', ''),
            }

        # Add to IPFS
        ipfs_hash = self.ipfs_handler.add_to_ipfs(formatted_data)
        if ipfs_hash:
            formatted_data["ipfs_hash"] = ipfs_hash
        
        return formatted_data

    def scrape_patents(self, patent_numbers: List[str]) -> pd.DataFrame:
        """Scrape multiple patents and save to IPFS"""
        all_patents = []
        
        for patent_number in patent_numbers:
            logging.info(f"Scraping patent {patent_number}")
            
            # Try Google Patents first
            patent_data = self.scrape_google_patent(patent_number)
            
            # If Google Patents fails, try USPTO
            if not patent_data:
                patent_data = self.scrape_uspto(patent_number)
            
            if patent_data:
                # Save to IPFS and get formatted data
                ipfs_data = self.save_to_ipfs(patent_data)
                all_patents.append(ipfs_data)
            
            time.sleep(2)
        
        return pd.DataFrame(all_patents)

def clean_html_content(url: str) -> Dict:
    """
    Fetch and extract content from HTML elements
    Returns a dictionary with all patent information except claims
    """
    try:
        # Setup Chrome options for Linux
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--remote-debugging-port=9222")
        chrome_options.binary_location = "/snap/bin/chromium"  # Linux Chromium path

        # Initialize the driver with Linux ChromeDriver path
        driver = webdriver.Chrome(
            service=Service("/snap/chromium/current/usr/lib/chromium-browser/chromedriver"),
            options=chrome_options
        )
        
        logging.info(f"Fetching content from URL: {url}")
        driver.get(url)
        response_text = driver.page_source
        driver.quit()
        
        # Parse HTML
        soup = BeautifulSoup(response_text, 'html.parser')
        
        # Create HTML 2 text convertor for patent_text
        h = html2text.HTML2Text()
        h.ignore_links = True  # Ignore hyperlinks
        h.ignore_images = True  # Ignore images
        h.ignore_tables = False  # Keep tables as they might contain important data
        h.body_width = 0  # Don't wrap text at a certain width
        
        # Extract all relevant sections
        claims = soup.find('section', itemprop='claims')
        abstract = soup.find('section', itemprop='abstract')
        inventor = soup.find('dd', itemprop='inventor')
        assigneeCurrent = soup.find('dd', itemprop='assigneeCurrent')
        assigneeOriginal = soup.find('dd', itemprop='assigneeOriginal')
        filingDate = soup.find('time', itemprop='filingDate')
        title = soup.find('span', itemprop='title')
        
        # Process full text
        full_text = h.handle(str(soup))
        full_text = '\n'.join(line.strip() for line in full_text.splitlines() if line.strip())
        
        # Process claims text
        claims_text = h.handle(str(claims)) if claims else None
        if claims_text:
            claims_text = '\n'.join(line.strip() for line in claims_text.splitlines() if line.strip())
        
        # Create structured data
        patent_data = {
            'patent_title': title.text.strip() if title else 'N/A',
            'abstract': abstract.text.strip() if abstract else 'N/A',
            'inventor_name': inventor.text.strip() if inventor else 'N/A',
            'assignee_name': (assigneeCurrent.text.strip() if assigneeCurrent else 
                            assigneeOriginal.text.strip() if assigneeOriginal else 'N/A'),
            'filing_date': filingDate.text.strip() if filingDate else 'N/A',
            'claims_text': claims_text,
            "patent_text": full_text
        }
        return patent_data
        
    except Exception as e:
        logging.error(f"Error extracting HTML content: {e}")
        return None

def extract_patent_info_with_llm(url):
    # Get structured data and claims text
    patent_data = clean_html_content(url)
    if not patent_data:
        logging.error(f"Failed to fetch patent data for URL: {url}")
        return None
    
    # Initialize patent_info with the basic data
    patent_info = patent_data.copy()
    patent_info['patent_url'] = url
    
    # Extract publication number from URL
    pattern = r"/patent/([^/]+)/"
    match = re.search(pattern, url)
    patent_info['publication_number'] = match.group(1) if match else 'N/A'
    
    # Store claims text in inventions if available, otherwise empty list
    patent_info['inventions'] = [patent_data['claims_text']] if patent_data['claims_text'] else []
    
    # Remove the raw claims text
    patent_info.pop('claims_text', None)
    return patent_info

def read_patent_urls_from_file(filename="test_url.txt"):
    """Read patent URLs from the text file"""
    try:
        with open(filename, 'r') as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        logging.error(f"Error: {filename} not found. Please run getlinks.py first to generate the file.")
        return []

async def generate_embeddings(text, model_name="all-MiniLM-L6-v2"):
    logging.info(f"Generating embeddings using model: {model_name}")
    embedding = model.encode(text, convert_to_numpy=True)
    logging.info("Generated embedding")
    return embedding

async def store_in_chromadb(text, embedding, collection_name="patents_collection"):
    logging.info(f"Storing data in ChromaDB collection: {collection_name}")
    client = chromadb.PersistentClient(path="./chromadb_store")
    collection = client.get_or_create_collection(collection_name, metadata={"hnsw:space": "cosine"}, embedding_function=None)

    doc_id = str(uuid.uuid4())
    collection.add(
        ids=[doc_id],
        embeddings=[embedding.tolist()],
        documents=[text],
    )
    logging.info(f"Added document with ID {doc_id} to ChromaDB.")
    return doc_id

def process_patents():
    logging.info("Starting patent processing")
    try:
        # Your existing processing code here
        logging.info("Processing patent data...")
        # ...existing processing code...
        
    except Exception as e:
        logging.error(f"Error processing patents: {str(e)}")
    finally:
        logging.info("Patent processing completed")

# Example usage
def main():
    # Initialize IPFS handler
    ipfs_handler = IPFSHandler()
    
    # Create patent_json directory if it doesn't exist
    if not os.path.exists('patent_json'):
        os.makedirs('patent_json')
        logging.info("Created patent_json directory")
    
    # Read patent URLs from the text file instead of calling getLinks
    patent_urls = read_patent_urls_from_file()
    if not patent_urls:
        logging.info("No patent URLs found in patent_urls.txt. Exiting...")
        return
        
    logging.info(f"Found {len(patent_urls)} patent URLs to process from patent_urls.txt")
    
    processed_patents = []
    skipped_patents = []
    
    for url in patent_urls:
        # Clean the URL (remove @ symbol if present)
        url = url.strip().replace("@", "")
        logging.info(f"\nProcessing URL: {url}")
        
        try:
            # Updated regex pattern to match the exact format
            pattern = r"/patent/([A-Z0-9]+)"

            match = re.search(pattern, url)
            patent_no = match.group(1)
            
            if not patent_no:
                logging.info("Invalid URL format. Skipping...")
                skipped_patents.append(url)
                continue
            
            # Change this line to use proper string formatting
            logging.info(f"Processing patent number: {patent_no}")

            # Check if JSON already exists
            json_path = f"patent_json/{patent_no}.json"
            if os.path.exists(json_path):
                logging.info(f"Patent {patent_no} already exists in local storage, skipping...")
                skipped_patents.append(patent_no)
                continue

            # Extract patent information using LLM
            patent_data = extract_patent_info_with_llm(url)
            # logging.info(patent_data)
            if patent_data:
                # Get patent number from the data
                # patent_number = patent_data.get('publication_number', '')
                patent_data["publication_number"] = patent_no
                
                # Save JSON file locally
                # json_path = f"patent_json/{patent_number}.json"
                try:
                    with open(json_path, 'w', encoding='utf-8') as f:
                        json.dump(patent_data, f, indent=4, ensure_ascii=False)
                    logging.info(f"Saved JSON file locally: {json_path}")
                    
                    # Generate embeddings and store in ChromaDB
                    text = json.dumps(patent_data)  # Convert patent data to string
                    embedding = asyncio.run(generate_embeddings(text))
                    doc_id = asyncio.run(store_in_chromadb(text, embedding))
                    logging.info(f"Successfully stored in ChromaDB with ID: {doc_id}")
                        
                except Exception as e:
                    logging.error(f"Error saving JSON file or storing in ChromaDB: {e}")
                    continue
                
                # Save to IPFS and get the hash
                logging.info(f"Uploading patent {patent_no} to IPFS...")
                ipfs_hash = ipfs_handler.save_and_upload(patent_data, patent_no)
                
                if ipfs_hash:
                    logging.info(f"Successfully processed patent {patent_no}")
                    logging.info(f"IPFS Hash: {ipfs_hash}")
                    logging.info(f"Local JSON file saved in: {json_path}")
                    processed_patents.append(patent_no)
                else:
                    logging.info(f"Failed to upload patent {patent_no} to IPFS")
            else:
                logging.info("Failed to extract patent information")
                
        except Exception as e:
            logging.error(f"Error processing patent URL {url}: {str(e)}")
            continue
            
        # Add a small delay between requests to avoid rate limiting
        time.sleep(2)
    
    # Print summary
    logging.info("\nProcessing Summary:")
    logging.info(f"Total patents processed: {len(processed_patents)}")
    logging.info(f"Patents skipped (already existed): {len(skipped_patents)}")
    if processed_patents:
        logging.info("\nSuccessfully processed patents:")
        for patent in processed_patents:
            logging.info(f"- {patent}")
    if skipped_patents:
        logging.info("\nSkipped patents (already existed):")
        for patent in skipped_patents:
            logging.info(f"- {patent}")

# Run the extraction
if __name__ == "__main__":
    main()
