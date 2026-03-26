import os
import json
import logging
import time
from datetime import datetime
from time import mktime
import feedparser
import google.generativeai as genai
from sqlalchemy.orm import Session
from database import SessionLocal, MarketSignal, ImpactLevel
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("Critical Error: GEMINI_API_KEY is missing from environment variables.")

genai.configure(api_key=GEMINI_API_KEY)

# THE "WIDER NET": 11 Highly Targeted Feeds
RSS_FEEDS = [
    {"name": "ET Realty - Residential", "url": "https://realty.economictimes.indiatimes.com/rss/residential"},
    {"name": "ET Realty - Commercial", "url": "https://realty.economictimes.indiatimes.com/rss/commercial"},
    {"name": "ET Realty - Infrastructure", "url": "https://realty.economictimes.indiatimes.com/rss/infrastructure"},
    {"name": "Hindustan Times - Property", "url": "https://www.hindustantimes.com/feeds/rss/real-estate/rssfeed.xml"},
    {"name": "Moneycontrol - Real Estate", "url": "https://www.moneycontrol.com/rss/realestate.xml"},
    {"name": "Financial Express - Real Estate", "url": "https://www.financialexpress.com/money/real-estate/feed/"},
    {"name": "Google News - Noida RERA", "url": "https://news.google.com/rss/search?q=noida+rera+real+estate&hl=en-IN&gl=IN&ceid=IN:en"},
    {"name": "Google News - Dwarka Expressway", "url": "https://news.google.com/rss/search?q=dwarka+expressway+property&hl=en-IN&gl=IN&ceid=IN:en"},
    {"name": "Google News - Yamuna Expressway", "url": "https://news.google.com/rss/search?q=yamuna+expressway+land+acquisition&hl=en-IN&gl=IN&ceid=IN:en"},
    {"name": "Google News - Gurgaon Real Estate", "url": "https://news.google.com/rss/search?q=gurgaon+real+estate&hl=en-IN&gl=IN&ceid=IN:en"},
    {"name": "Google News - Delhi DDA", "url": "https://news.google.com/rss/search?q=dda+delhi+real+estate&hl=en-IN&gl=IN&ceid=IN:en"}
]

SYSTEM_PROMPT = """
You are an elite real estate intelligence analyst for the Delhi NCR market (Delhi, Noida, Gurgaon, Yamuna Expressway). 
Analyze this batch of news articles. If an article is NOT about NCR real estate, IGNORE IT completely.

For relevant articles, return a JSON array. Follow this strict schema:
[
    {
        "batch_id": "Exact ID integer provided in the input.",
        "headline": "A punchy, 4-6 word financial broadsheet headline (e.g., 'DDA Offers 25% Flat Discount' or 'Accenture Leases 1.65L SqFt').",
        "location": "Hyper-local market (e.g., 'Sector 150, Noida', 'SPR, Gurgaon').",
        "category": "Use one: 'Infrastructure', 'Policy Shift', 'Commercial', 'Residential', 'Land Acquisition'.",
        "impact": "Must be exactly: 'Positive', 'Negative', or 'Neutral'.",
        "summary": "Write exactly 2 crisp, financial-broadsheet style sentences. Sentence 1: The core fact (who/what/where). Sentence 2: The direct impact on local property yields, rental demand, or capital appreciation. NO fluff. NO emojis. Be clinical and authoritative."
    }
]
"""

def is_relevant_for_ncr(text: str) -> bool:
    """Zero-Cost Python Pre-Filter."""
    text = text.lower()
    target_keywords = [
        'delhi', 'ncr', 'new delhi', 'noida', 'gurgaon', 'gurugram', 
        'faridabad', 'ghaziabad', 'greater noida', 'sonipat', 'meerut',
        'rohtak', 'manesar', 'bhiwadi', 'sohna', 'palwal',
        'jewar', 'dwarka expressway', 'yamuna expressway', 'okhla',
        'vasant vihar', 'dlf', 'aerocity', 'golf course road', 'spr',
        'southern peripheral road', 'npr', 'kmp expressway',
        'rera', 'up rera', 'hrera', 'dda', 'gmda', 'noida authority', 
        'yeida', 'gnida', 'dtcp', 'mcd', 'ndmc'
    ]
    return any(keyword in text for keyword in target_keywords)

def process_batch_with_gemini(batch: list) -> list:
    if not batch:
        return []

    articles_text = "Articles to Analyze:\n\n"
    for item in batch:
        articles_text += f"--- ARTICLE ID: {item['batch_id']} ---\n"
        articles_text += f"Headline: {item['title']}\n"
        articles_text += f"Text: {item['text']}\n\n"

    try:
        # OPTIMIZATION: Switched to flash-lite for extreme efficiency
        model = genai.GenerativeModel('gemini-2.5-flash-lite')
        response = model.generate_content(f"{SYSTEM_PROMPT}\n{articles_text}")
        
        response_text = response.text.strip()
        
        if response_text.startswith("```json"):
            response_text = response_text[7:]
        elif response_text.startswith("```"):
            response_text = response_text[3:]
            
        if response_text.endswith("```"):
            response_text = response_text[:-3]
            
        results = json.loads(response_text.strip())
        
        if not isinstance(results, list):
            logging.error("Gemini did not return a JSON array.")
            return []
            
        return results
        
    except json.JSONDecodeError as e:
        logging.error(f"Failed to decode JSON from Gemini. Error: {e}")
        return []
    except Exception as e:
        logging.error(f"Gemini API Error: {str(e)}")
        return []

def fetch_and_process_feeds():
    db: Session = SessionLocal()
    
    # THE 15-DAY HORIZON: Perfect balance of volume and recency
    MAX_DAYS_OLD = 15 
    
    for feed_source in RSS_FEEDS:
        logging.info(f"Fetching RSS feed from: {feed_source['name']}")
        try:
            feed = feedparser.parse(feed_source['url'])
        except Exception as e:
            logging.error(f"Failed to parse feed {feed_source['name']}: {e}")
            continue
        
        current_batch = []
        
        for entry in feed.entries[:25]: # Checking top 25 per feed to ensure we hit the 15-day limit
            # 0. Time Horizon Filter
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                pub_date = datetime.fromtimestamp(mktime(entry.published_parsed))
                days_old = (datetime.utcnow() - pub_date).days
                if days_old > MAX_DAYS_OLD:
                    continue 
            
            # 1. Skip if already in DB
            exists = db.query(MarketSignal).filter(MarketSignal.source_url == entry.link).first()
            if exists:
                continue
                
            article_text = entry.summary if hasattr(entry, 'summary') else entry.title
            full_text_to_check = f"{entry.title} {article_text}"
            
            # 2. Python Pre-filter
            if not is_relevant_for_ncr(full_text_to_check):
                continue
            
            # 3. Add to batch
            current_batch.append({
                "batch_id": len(current_batch),
                "title": entry.title,
                "text": article_text,
                "url": entry.link,
                "source_name": feed_source['name'],
                "published_parsed": entry.published_parsed if hasattr(entry, 'published_parsed') else None
            })
            
            # 4. OPTIMIZATION: Process batch of 25 to protect Free Tier RPD
            if len(current_batch) == 25:
                logging.info("Processing massive batch of 25 articles...")
                process_and_save_batch(db, current_batch)
                current_batch = []
                time.sleep(25) # Keeps you safe under the 5 Requests/Min Free Tier limit
                
        # Process remaining
        if len(current_batch) > 0:
            logging.info(f"Processing final batch of {len(current_batch)} articles...")
            process_and_save_batch(db, current_batch)
            time.sleep(25)

    db.close()
    logging.info("RSS Feed processing cycle complete.")

def process_and_save_batch(db: Session, batch: list):
    ai_results = process_batch_with_gemini(batch)
    
    for result in ai_results:
        try:
            batch_id = int(result.get('batch_id', -1))
            if batch_id < 0 or batch_id >= len(batch):
                continue
                
            original_article = batch[batch_id]
            
            impact_enum = ImpactLevel.NEUTRAL
            impact_str = result.get('impact', '').capitalize()
            if impact_str in [item.value for item in ImpactLevel]:
                impact_enum = ImpactLevel(impact_str)

            pub_date = datetime.utcnow()
            if original_article['published_parsed']:
                pub_date = datetime.fromtimestamp(mktime(original_article['published_parsed']))

            # FIX: Properly extract the headline from Gemini's response
            new_signal = MarketSignal(
                headline=result.get('headline', 'Market Update'), 
                location=result.get('location', 'Delhi NCR'),
                category=result.get('category', 'Market Update'),
                impact=impact_enum,
                summary=result.get('summary', 'Market conditions are evolving. Check source for details.'),
                source_url=original_article['url'],
                source_name=original_article['source_name'],
                published_at=pub_date
            )
            db.add(new_signal)
            db.commit()
            logging.info(f"🟢 Saved curated signal for: {new_signal.headline}")
            
        except Exception as db_error:
            db.rollback()
            logging.error(f"Database insertion failed for a batch item: {str(db_error)}")

if __name__ == "__main__":
    fetch_and_process_feeds()