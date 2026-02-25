"""
fondoke-hotel-reviews Lambda Function
Built with FastAPI + Mangum for AWS Lambda
How it works:
- FastAPI handles routing and validation
- Mangum wraps FastAPI so AWS Lambda can trigger it
- DynamoDB caches Google Places API results for 3 days
"""
# ── Imports ───────────────────────────────────────────────────────────────────
import json        # Built-in: convert data to/from JSON format
import logging     # Built-in: write logs to CloudWatch
import os          # Built-in: read environment variables
import time        # Built-in: get current timestamp
import urllib.request  # Built-in: make HTTP calls to Google API
import urllib.error    # Built-in: handle HTTP errors
import boto3                          # AWS library: talk to DynamoDB
from botocore.exceptions import ClientError  # AWS error handling
from fastapi import FastAPI           # FastAPI: web framework
from fastapi.responses import JSONResponse   # FastAPI: send JSON back
from mangum import Mangum             # Mangum: Lambda + FastAPI adapter
from pydantic import BaseModel        # Pydantic: validate incoming data
from typing import Optional           # Python typing: optional fields


# ── Logger Setup ──────────────────────────────────────────────────────────────
# Creates a logger that writes to AWS CloudWatch
# LOG_LEVEL env var controls detail: DEBUG, INFO, WARNING, ERROR

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
# ── Environment Variables (All config comes from here — nothing hardcoded) ────
GOOGLE_API_KEY      = os.environ.get("GOOGLE_API_KEY", "")
DYNAMODB_TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME", "fondoke_reviews_external")
DYNAMODB_REGION     = os.environ.get("DYNAMODB_REGION", "eu-west-1")
CACHE_TTL_DAYS      = int(os.environ.get("CACHE_TTL_DAYS", "3"))
MAX_REVIEWS         = int(os.environ.get("MAX_REVIEWS", "10"))
PLACES_BASE_URL     = "https://places.googleapis.com/v1"
# ── DynamoDB Connection ───────────────────────────────────────────────────────
# Created once when Lambda starts (not on every request = faster)
dynamodb = boto3.resource("dynamodb", region_name=DYNAMODB_REGION)
table    = dynamodb.Table(DYNAMODB_TABLE_NAME)
# ── FastAPI App ───────────────────────────────────────────────────────────────
# This creates our FastAPI application
# title and description show up in the auto-generated API docs
app = FastAPI(
    title="Fondoke Hotel Reviews API",
    description="Fetches hotel reviews from Google Places API with DynamoDB caching",
    version="1.0.0"
)
# ── Pydantic Models (Request & Response shapes) ───────────────────────────────
class HotelReviewRequest(BaseModel):
    """
    Defines EXACTLY what fields the API accepts.
    Pydantic automatically validates types and required fields.
    If a required field is missing, FastAPI returns 422 automatically.
    """
    hotel_uuid:  str            # Required: Fondoke internal hotel ID
    hotel_name:  str            # Required: used to search Google
    city:        str            # Required: used to search Google
    country:     str            # Required: used to search Google
    latitude:    Optional[float] = None  # Optional: improves Google search accuracy
    longitude:   Optional[float] = None  # Optional: improves Google search accuracy
class ReviewItem(BaseModel):
    """Shape of a single review returned in the response."""
    author_name:   str
    author_url:    str
    rating:        Optional[int]
    text:          str
    language:      str
    time:          str
    relative_time: str
class HotelReviewResponse(BaseModel):
    """
    Defines EXACTLY what the API returns.
    FastAPI uses this to auto-generate API documentation.
    """
    hotel_uuid:   str
    total_count:  int
    rating:       str
    reviews:      list
    source:       str   # "cache" or "google"
    last_updated: int   # Unix timestamp
# ── API Routes ────────────────────────────────────────────────────────────────
@app.get("/")
def health_check():
    """
    Health check endpoint.
    Call GET / to verify the API is alive.
    Returns: {"status": "healthy", "service": "fondoke-hotel-reviews"}
    """
    return {"status": "healthy", "service": "fondoke-hotel-reviews"}
@app.post("/reviews", response_model=HotelReviewResponse)
def get_hotel_reviews(request: HotelReviewRequest):
    """
    Main endpoint: Get hotel reviews.
    Flow:
    1. Check DynamoDB for cached data (max 3 days old)
    2. If fresh cache exists → return it immediately
    3. If no cache or stale → call Google Places API
    4. Save Google data to DynamoDB
    5. Return data to caller
    Args:
        request: HotelReviewRequest with hotel_uuid, hotel_name, city, country
    Returns:
        HotelReviewResponse with total_count, rating, reviews, source
    """
    logger.info(
        "Request received: hotel_uuid=%s hotel_name=%s city=%s country=%s",
        request.hotel_uuid, request.hotel_name, request.city, request.country
    )
    # ── Step 1: Check DynamoDB cache ──────────────────────────────────────────
    cached = _get_cached(request.hotel_uuid)
    if cached:
        # Fresh data found in DynamoDB — return it without calling Google
        logger.info("Cache hit for hotel_uuid=%s", request.hotel_uuid)
        return HotelReviewResponse(
            hotel_uuid   = request.hotel_uuid,
            total_count  = int(cached["total_count"]),
            rating       = cached["rating"],
            reviews      = cached.get("reviews", []),
            source       = "cache",
            last_updated = int(cached["created_at"])
        )
    # ── Step 2: Cache miss — call Google Places API ───────────────────────────
    logger.info("Cache miss for hotel_uuid=%s — calling Google", request.hotel_uuid)
    try:
        google_data = _fetch_from_google(
            hotel_name = request.hotel_name,
            city       = request.city,
            country    = request.country,
            latitude   = request.latitude,
            longitude  = request.longitude
        )
    except Exception as exc:
        logger.error("Google API error for hotel_uuid=%s: %s", request.hotel_uuid, exc)
        return JSONResponse(
            status_code = 502,
            content     = {"error": f"Failed to fetch from Google: {str(exc)}"}
        )
    # ── Step 3: Save to DynamoDB ──────────────────────────────────────────────
    try:
        _save_to_dynamo(request.hotel_uuid, google_data)
    except Exception as exc:
        # DynamoDB write failed — still return the data, just warn
        logger.error("DynamoDB write failed for hotel_uuid=%s: %s", request.hotel_uuid, exc)
        return HotelReviewResponse(
            hotel_uuid   = request.hotel_uuid,
            total_count  = google_data["total_count"],
            rating       = google_data["rating"],
            reviews      = google_data["reviews"],
            source       = "google",
            last_updated = google_data["fetched_at"]
        )
    # ── Step 4: Return fresh Google data ─────────────────────────────────────
    logger.info(
        "Saved to DynamoDB hotel_uuid=%s rating=%s count=%s",
        request.hotel_uuid, google_data["rating"], google_data["total_count"]
    )
    return HotelReviewResponse(
        hotel_uuid   = request.hotel_uuid,
        total_count  = google_data["total_count"],
        rating       = google_data["rating"],
        reviews      = google_data["reviews"],
        source       = "google",
        last_updated = google_data["fetched_at"]
    )
# ── DynamoDB Helper Functions ─────────────────────────────────────────────────
def _get_cached(hotel_uuid: str) -> dict | None:
    """
    Get item from DynamoDB if it exists and is not stale.
    Args:
        hotel_uuid: The Fondoke hotel ID (DynamoDB partition key)
    Returns:
        dict with cached data if fresh, None if not found or stale
    """
    try:
        response = table.get_item(Key={"hotel_uuid": hotel_uuid})
    except ClientError as e:
        logger.error("DynamoDB get_item error: %s", e)
        raise
    item = response.get("Item")
    # No record found in DynamoDB
    if not item:
        logger.debug("No DynamoDB record for hotel_uuid=%s", hotel_uuid)
        return None
    # Check if record is still fresh (within TTL)
    created_at   = int(item.get("created_at", 0))
    age_seconds  = time.time() - created_at
    ttl_seconds  = CACHE_TTL_DAYS * 86400  # 86400 = seconds in a day
    if age_seconds > ttl_seconds:
        logger.info(
            "Stale cache for hotel_uuid=%s (age=%.0f seconds, ttl=%s seconds)",
            hotel_uuid, age_seconds, ttl_seconds
        )
        return None
    return item
def _save_to_dynamo(hotel_uuid: str, data: dict) -> None:
    """
    Insert or replace hotel review record in DynamoDB.
    Args:
        hotel_uuid: The Fondoke hotel ID
        data: Dict with total_count, rating, reviews, fetched_at
    """
    item = {
        "hotel_uuid":  hotel_uuid,
        "total_count": data["total_count"],
        "rating":      data["rating"],
        "reviews":     data["reviews"],
        "created_at":  int(time.time())
    }
    table.put_item(Item=item)
# ── Google Places API Helper Functions ───────────────────────────────────────
def _fetch_from_google(
    hotel_name: str,
    city: str,
    country: str,
    latitude: float  = None,
    longitude: float = None
) -> dict:
    """
    Full Google Places flow:
    1. Text Search → find the Place ID
    2. Place Details → get rating, count, reviews
    Returns:
        dict with total_count, rating, reviews, fetched_at
    """
    # Step 1: Find the Google Place ID for this hotel
    place_id = _search_place(hotel_name, city, country, latitude, longitude)
    if not place_id:
        raise ValueError(
            f"Hotel '{hotel_name}' in {city}, {country} not found in Google Places"
        )
    # Step 2: Get the full details including reviews
    details = _get_place_details(place_id)
    # Step 3: Convert Google format to our format
    return _normalize(details)
def _search_place(
    hotel_name: str,
    city: str,
    country: str,
    latitude: float  = None,
    longitude: float = None
) -> str | None:
    """
    Call Google Text Search API to find the Place ID.
    Google Text Search takes a text query like:
    "The Shelbourne Hotel Dublin Ireland"
    and returns matching places with their Place IDs.
    Place ID example: "ChIJxxxxxxxxxxxxxxxxx"
    """
    # Build the search query
    payload = {
        "textQuery":      f"{hotel_name} {city} {country}",
        "includedType":   "lodging",    # only search hotels/lodging
        "languageCode":   "en",         # return results in English
        "maxResultCount": 1             # we only need the top result
    }
    # If coordinates provided, bias Google search to that location
    # This improves accuracy when hotel name is common
    if latitude is not None and longitude is not None:
        payload["locationBias"] = {
            "circle": {
                "center": {
                    "latitude":  latitude,
                    "longitude": longitude
                },
                "radius": 500.0  # search within 500 metres of the hotel
            }
        }
    # Call Google Text Search API
    result = _google_post(
        endpoint   = "places:searchText",
        payload    = payload,
        field_mask = "places.id,places.displayName"
        # field_mask: only request fields we need (Google charges per field)
    )
    places = result.get("places", [])
    if not places:
        logger.warning("No places found for query: %s %s %s", hotel_name, city, country)
        return None
    place_id = places[0].get("id")
    logger.info("Found place_id=%s for %s", place_id, hotel_name)
    return place_id
def _get_place_details(place_id: str) -> dict:
    """
    Call Google Place Details API to get full hotel information.
    Fields requested:
    - id: the place ID
    - rating: average star rating (e.g. 4.6)
    - userRatingCount: total number of reviews (e.g. 3840)
    - reviews: list of recent reviews (Google returns max 5)
    """
    return _google_get(
        endpoint   = f"places/{place_id}",
        field_mask = "id,rating,userRatingCount,reviews"
    )
def _normalize(details: dict) -> dict:
    """
    Convert Google Places API response format to our own format.
    Google review format → Our format:
    authorAttribution.displayName → author_name
    authorAttribution.uri         → author_url
    rating                        → rating
    text.text                     → text
    text.languageCode             → language
    publishTime                   → time
    relativePublishTimeDescription → relative_time
    """
    raw_reviews = details.get("reviews", [])
    reviews = []
    # Process each review (limit to MAX_REVIEWS)
    for rev in raw_reviews[:MAX_REVIEWS]:
        # Get review text (try "text" first, fall back to "originalText")
        text_obj = rev.get("text") or rev.get("originalText") or {}
        # Get author information
        author = rev.get("authorAttribution", {})
        reviews.append({
            "author_name":   author.get("displayName", "Anonymous"),
            "author_url":    author.get("uri", ""),
            "rating":        rev.get("rating"),           # star rating 1-5
            "text":          text_obj.get("text", ""),    # review text
            "language":      text_obj.get("languageCode", ""),
            "time":          rev.get("publishTime", ""),  # ISO timestamp
            "relative_time": rev.get("relativePublishTimeDescription", "")
            # relative_time example: "a month ago"
        })
    return {
        "total_count": details.get("userRatingCount", 0),
        "rating":      str(round(float(details.get("rating", 0.0)), 1)),
        # rating: convert to string rounded to 1 decimal e.g. "4.6"
        "reviews":     reviews,
        "fetched_at":  int(time.time())  # Unix timestamp of when we fetched
    }
# ── HTTP Helper Functions ─────────────────────────────────────────────────────
def _google_post(endpoint: str, payload: dict, field_mask: str) -> dict:
    """
    Make a POST request to Google Places API.
    Args:
        endpoint:   API path e.g. "places:searchText"
        payload:    Request body as dict
        field_mask: Comma-separated fields to return (controls billing)
    """
    url  = f"{PLACES_BASE_URL}/{endpoint}"
    data = json.dumps(payload).encode("utf-8")  # convert dict to JSON bytes
    headers = {
        "Content-Type":     "application/json",
        "X-Goog-Api-Key":   GOOGLE_API_KEY,   # authenticate with Google
        "X-Goog-FieldMask": field_mask         # only return requested fields
    }
    return _http_request(url, headers, data, "POST")


def _google_get(endpoint: str, field_mask: str) -> dict:
    """
    Make a GET request to Google Places API.
    Args:
        endpoint:   API path e.g. "places/ChIJxxxxx"
        field_mask: Comma-separated fields to return
    """
    url = f"{PLACES_BASE_URL}/{endpoint}"
    headers = {
        "X-Goog-Api-Key":   GOOGLE_API_KEY,
        "X-Goog-FieldMask": field_mask
    }
    return _http_request(url, headers, None, "GET")


def _http_request(url: str, headers: dict, data: bytes | None, method: str) -> dict:
    """
    Make an HTTP request and return the JSON response.
    Args:
        url:     Full URL to call
        headers: HTTP headers dict
        data:    Request body bytes (None for GET)
        method:  "GET" or "POST"
    Raises:
        Exception: with descriptive message on any HTTP or network error
    """
    req = urllib.request.Request(
        url,
        data    = data,
        headers = headers,
        method  = method
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            # Read response body and decode from bytes to string
            body = response.read().decode("utf-8")
            # Parse JSON string into Python dict and return
            return json.loads(body)
    except urllib.error.HTTPError as e:
        # Google returned an error (e.g. 403 invalid key, 404 not found)
        error_body = e.read().decode("utf-8", errors="replace")
        raise Exception(f"Google HTTP {e.code}: {error_body}")
    except urllib.error.URLError as e:
        # Network error (e.g. no internet, timeout)
        raise Exception(f"Network error: {e.reason}")
    except json.JSONDecodeError as e:
        # Google returned something that isn't valid JSON
        raise Exception(f"Invalid JSON from Google: {e}")
# ── Mangum Handler (THIS is what AWS Lambda actually calls) ───────────────────
mangum_handler  = Mangum(app, lifespan="off")
# Mangum wraps our FastAPI app so AWS Lambda can trigger it.
# When Lambda receives an event, it calls handler(event, context)
# Mangum converts the Lambda event to an HTTP request FastAPI understands
# Then converts FastAPI's HTTP response back to Lambda's expected format
# lifespan="off" disables startup/shutdown events (not needed in Lambda)


def lambda_handler(event, context):
    """
    Wrapper to handle both API Gateway events and direct invocations
    """
    logger.debug(f"Received event: {json.dumps(event)}")
    logger.debug(f"Context: {context}")
    
    # Check if this looks like an API Gateway event
    if event.get("httpMethod") and event.get("path"):
        # This is an API Gateway event - use Mangum
        logger.info("Processing as API Gateway event")
        return mangum_handler(event, context)
    else:
        # This is a direct invocation - return a helpful message
        logger.info("Processing as direct invocation")
        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json"
            },
            "body": json.dumps({
                "message": "This Lambda expects to be triggered via API Gateway with HTTP requests",
                "usage": {
                    "health_check": {
                        "method": "GET",
                        "path": "/",
                        "example": "curl -X GET https://your-api-gateway-url/"
                    },
                    "get_reviews": {
                        "method": "POST", 
                        "path": "/reviews",
                        "body": {
                            "hotel_uuid": "test-hotel-001",
                            "hotel_name": "The Shelbourne Hotel",
                            "city": "Dublin",
                            "country": "Ireland"
                        },
                        "example": "curl -X POST https://your-api-gateway-url/reviews -H 'Content-Type: application/json' -d '{\"hotel_uuid\":\"test-hotel-001\",\"hotel_name\":\"The Shelbourne Hotel\",\"city\":\"Dublin\",\"country\":\"Ireland\"}'"
                    }
                },
                "received_event": event
            })
        }