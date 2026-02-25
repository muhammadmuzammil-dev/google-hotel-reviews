import json
import logging
import os
import time
import urllib.request
import urllib.error
import boto3
from decimal import Decimal
from botocore.exceptions import ClientError
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
# ── Environment Variables ─────────────────────────────────────────────────────
GOOGLE_API_KEY      = os.environ.get("GOOGLE_API_KEY", "")
DYNAMODB_TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME", "fondoke_reviews_external")
DYNAMODB_REGION     = os.environ.get("DYNAMODB_REGION", "eu-west-1")
CACHE_TTL_DAYS      = int(os.environ.get("CACHE_TTL_DAYS", "3"))
MAX_REVIEWS         = int(os.environ.get("MAX_REVIEWS", "10"))
PLACES_BASE_URL     = "https://places.googleapis.com/v1"
# ── DynamoDB client ───────────────────────────────────────────────────────────
dynamodb = boto3.resource("dynamodb", region_name=DYNAMODB_REGION)
table    = dynamodb.Table(DYNAMODB_TABLE_NAME)
def lambda_handler(event, context):
    """
    Main Lambda entry point.
    Expects event with: hotel_uuid, hotel_name, city, country
    Optional: latitude, longitude
    """
    logger.info("Event received: %s", json.dumps(event))
    # Parse body (API Gateway sends body as string)
    body = _parse_body(event)
    # Validate required fields
    for field in ["hotel_uuid", "hotel_name", "city", "country"]:
        if not body.get(field):
            return _response(400, {"error": f"Missing required field: {field}"})
    hotel_uuid = str(body["hotel_uuid"]).strip()
    hotel_name = str(body["hotel_name"]).strip()
    city       = str(body["city"]).strip()
    country    = str(body["country"]).strip()
    latitude   = body.get("latitude")
    longitude  = body.get("longitude")
    # ── Check DynamoDB cache ──────────────────────────────────────────────────
    cached = _get_cached(hotel_uuid)
    if cached:
        logger.info("Cache hit for hotel_uuid=%s", hotel_uuid)
        return _response(200, {
            "hotel_uuid":  hotel_uuid,
            "total_count": int(cached["total_count"]),
            "rating":      cached["rating"],
            "reviews":     cached.get("reviews", []),
            "source":      "cache",
            "last_updated": int(cached["created_at"])
        })
    # ── Fetch from Google ─────────────────────────────────────────────────────
    logger.info("Cache miss. Fetching from Google for hotel_uuid=%s", hotel_uuid)
    try:
        data = _fetch_from_google(hotel_name, city, country, latitude, longitude)
    except Exception as exc:
        logger.error("Google API error: %s", exc)
        return _response(502, {"error": f"Google API error: {str(exc)}"})
    # ── Save to DynamoDB ──────────────────────────────────────────────────────
    try:
        _save_to_dynamo(hotel_uuid, data)
    except Exception as exc:
        logger.error("DynamoDB write error: %s", exc)
        return _response(200, {
            "hotel_uuid":  hotel_uuid,
            "total_count": data["total_count"],
            "rating":      data["rating"],
            "reviews":     data["reviews"],
            "source":      "google",
            "last_updated": data["fetched_at"],
            "warning":     "Fetched from Google but cache write failed"
        })
    return _response(200, {
        "hotel_uuid":  hotel_uuid,
        "total_count": data["total_count"],
        "rating":      data["rating"],
        "reviews":     data["reviews"],
        "source":      "google",
        "last_updated": data["fetched_at"]
    })
# ── DynamoDB helpers ──────────────────────────────────────────────────────────
def _get_cached(hotel_uuid):
    """Return item if it exists and is not older than CACHE_TTL_DAYS."""
    try:
        resp = table.get_item(Key={"hotel_uuid": hotel_uuid})
    except ClientError as e:
        logger.error("DynamoDB get error: %s", e)
        raise
    item = resp.get("Item")
    if not item:
        return None
    age = time.time() - int(item.get("created_at", 0))
    if age > CACHE_TTL_DAYS * 86400:
        logger.info("Stale cache for hotel_uuid=%s", hotel_uuid)
        return None
    return item
def _save_to_dynamo(hotel_uuid, data):
    """Insert or replace review record."""
    item = {
        "hotel_uuid":  hotel_uuid,
        "total_count": data["total_count"],
        "rating":      data["rating"],
        "reviews":     data["reviews"],
        "created_at":  int(time.time())
    }
    table.put_item(Item=item)
    logger.info("Saved to DynamoDB hotel_uuid=%s", hotel_uuid)
# ── Google Places API helpers ─────────────────────────────────────────────────
def _fetch_from_google(hotel_name, city, country, latitude=None, longitude=None):
    """Search for hotel and get its reviews."""
    place_id = _search_place(hotel_name, city, country, latitude, longitude)
    if not place_id:
        raise ValueError(f"Hotel '{hotel_name}' not found in Google Places")
    details = _get_place_details(place_id)
    return _normalize(details)
def _search_place(hotel_name, city, country, latitude=None, longitude=None):
    """Call Text Search API to get Google Place ID."""
    payload = {
        "textQuery":     f"{hotel_name} {city} {country}",
        "includedType":  "lodging",
        "languageCode":  "en",
        "maxResultCount": 1
    }
    if latitude and longitude:
        payload["locationBias"] = {
            "circle": {
                "center": {"latitude": latitude, "longitude": longitude},
                "radius": 500.0
            }
        }
    result = _google_post(
        "places:searchText",
        payload,
        "places.id,places.displayName"
    )
    places = result.get("places", [])
    if not places:
        return None
    place_id = places[0].get("id")
    logger.info("Found place_id=%s", place_id)
    return place_id
def _get_place_details(place_id):
    """Call Place Details API to get rating, count, reviews."""
    return _google_get(
        f"places/{place_id}",
        "id,rating,userRatingCount,reviews"
    )
def _normalize(details):
    """Convert Google response to our format."""
    raw_reviews = details.get("reviews", [])
    reviews = []
    for rev in raw_reviews[:MAX_REVIEWS]:
        text_obj = rev.get("text") or rev.get("originalText") or {}
        author   = rev.get("authorAttribution", {})
        reviews.append({
            "author_name":   author.get("displayName", "Anonymous"),
            "author_url":    author.get("uri", ""),
            "rating":        rev.get("rating"),
            "text":          text_obj.get("text", ""),
            "language":      text_obj.get("languageCode", ""),
            "time":          rev.get("publishTime", ""),
            "relative_time": rev.get("relativePublishTimeDescription", "")
        })
    return {
        "total_count": details.get("userRatingCount", 0),
        "rating":      str(round(float(details.get("rating", 0)), 1)),
        "reviews":     reviews,
        "fetched_at":  int(time.time())
    }
# ── HTTP helpers ──────────────────────────────────────────────────────────────
def _google_post(endpoint, payload, field_mask):
    url  = f"{PLACES_BASE_URL}/{endpoint}"
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type":    "application/json",
        "X-Goog-Api-Key":  GOOGLE_API_KEY,
        "X-Goog-FieldMask": field_mask
    }
    return _http(url, headers, data, "POST")
def _google_get(endpoint, field_mask):
    url = f"{PLACES_BASE_URL}/{endpoint}"
    headers = {
        "X-Goog-Api-Key":  GOOGLE_API_KEY,
        "X-Goog-FieldMask": field_mask
    }
    return _http(url, headers, None, "GET")
def _http(url, headers, data, method):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise Exception(f"HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        raise Exception(f"Network error: {e}")
# ── Response helper ───────────────────────────────────────────────────────────
def _parse_body(event):
    if "body" in event:
        body = event["body"]
        return json.loads(body) if isinstance(body, str) else body
    return event
def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers":    {"Content-Type": "application/json"},
        "body":       json.dumps(body, default=str)
    }