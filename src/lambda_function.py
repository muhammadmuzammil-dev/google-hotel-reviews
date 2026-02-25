"""
fondoke-hotel-reviews Lambda Function
Built with FastAPI + Mangum for AWS Lambda
"""
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Optional, List, Dict, Any

import boto3
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from mangum import Mangum
from pydantic import BaseModel, Field, model_validator


# ── Logger Setup ──────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ── Environment Variables ────────────────────────────────────────────────────
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    logger.error("GOOGLE_API_KEY environment variable not set")

DYNAMODB_TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME", "fondoke_reviews_external")
DYNAMODB_REGION = os.environ.get("DYNAMODB_REGION", "eu-west-1")
CACHE_TTL_DAYS = int(os.environ.get("CACHE_TTL_DAYS", "3"))
MAX_REVIEWS = int(os.environ.get("MAX_REVIEWS", "10"))
PLACES_BASE_URL = "https://places.googleapis.com/v1"

# ── DynamoDB Connection ───────────────────────────────────────────────────────
dynamodb = boto3.resource("dynamodb", region_name=DYNAMODB_REGION)
table = dynamodb.Table(DYNAMODB_TABLE_NAME)

# ── Pydantic Models ───────────────────────────────────────────────────────────
class HotelReviewRequest(BaseModel):
    hotel_uuid: str = Field(..., description="Fondoke internal hotel ID")
    hotel_name: str = Field(..., description="Hotel name")
    city: str = Field(..., description="City where hotel is located")
    country: str = Field(..., description="Country where hotel is located")
    latitude: Optional[float] = Field(None, description="Hotel latitude (optional)")
    longitude: Optional[float] = Field(None, description="Hotel longitude (optional)")
    
    @model_validator(mode='after')
    def validate_coordinates(self) -> 'HotelReviewRequest':
        """Validate coordinates"""
        lat = self.latitude
        lng = self.longitude
        
        # Check if one is provided without the other
        if (lat is None) != (lng is None):
            raise ValueError('Both latitude and longitude must be provided together')
        
        # Validate ranges if provided
        if lat is not None:
            if lat < -90 or lat > 90:
                raise ValueError('Latitude must be between -90 and 90')
        
        if lng is not None:
            if lng < -180 or lng > 180:
                raise ValueError('Longitude must be between -180 and 180')
        
        return self

class ReviewItem(BaseModel):
    author_name: str = ""
    author_url: str = ""
    rating: Optional[int] = None
    text: str = ""
    language: str = ""
    time: str = ""
    relative_time: str = ""

class HotelReviewResponse(BaseModel):
    hotel_uuid: str
    total_count: int
    rating: str
    reviews: List[ReviewItem]
    source: str  # "cache" or "google"
    last_updated: int

# ── FastAPI App ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="Fondoke Hotel Reviews API",
    description="Fetches hotel reviews from Google Places API with DynamoDB caching",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# ── API Routes ────────────────────────────────────────────────────────────────
@app.get("/")
def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "fondoke-hotel-reviews",
        "timestamp": int(time.time())
    }

@app.get("/health")
def health_check_alt():
    """Alternative health check endpoint"""
    return {"status": "healthy"}

@app.post("/reviews", response_model=HotelReviewResponse)
async def get_hotel_reviews(request: HotelReviewRequest):
    """
    Get hotel reviews from Google Places API with DynamoDB caching
    """
    logger.info(
        f"Request received: hotel_uuid={request.hotel_uuid}, "
        f"hotel_name={request.hotel_name}, city={request.city}, country={request.country}"
    )
    
    # ── Step 1: Check DynamoDB cache ──────────────────────────────────────────
    try:
        cached = await get_cached_reviews(request.hotel_uuid)
        if cached:
            logger.info(f"Cache hit for hotel_uuid={request.hotel_uuid}")
            return HotelReviewResponse(
                hotel_uuid=request.hotel_uuid,
                total_count=cached["total_count"],
                rating=cached["rating"],
                reviews=cached["reviews"],
                source="cache",
                last_updated=cached["created_at"]
            )
    except Exception as e:
        logger.error(f"Error checking cache: {e}")
        # Continue to Google API if cache check fails
    
    # ── Step 2: Fetch from Google Places API ──────────────────────────────────
    try:
        logger.info(f"Cache miss for hotel_uuid={request.hotel_uuid} - calling Google")
        google_data = await fetch_from_google(request)
        
        # ── Step 3: Save to DynamoDB (don't await - fire and forget) ──────────
        try:
            await save_to_dynamo(request.hotel_uuid, google_data)
        except Exception as e:
            logger.error(f"Failed to save to DynamoDB: {e}")
            # Continue even if save fails
        
        return HotelReviewResponse(
            hotel_uuid=request.hotel_uuid,
            total_count=google_data["total_count"],
            rating=google_data["rating"],
            reviews=google_data["reviews"],
            source="google",
            last_updated=int(time.time())
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching from Google: {e}", exc_info=True)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch from Google Places API: {str(e)}"
        )

# ── Helper Functions ─────────────────────────────────────────────────────────
async def get_cached_reviews(hotel_uuid: str) -> Optional[Dict[str, Any]]:
    """Get reviews from DynamoDB if they exist and are fresh"""
    try:
        response = table.get_item(Key={"hotel_uuid": hotel_uuid})
        item = response.get("Item")
        
        if not item:
            return None
        
        created_at = int(item.get("created_at", 0))
        age_seconds = time.time() - created_at
        ttl_seconds = CACHE_TTL_DAYS * 24 * 60 * 60
        
        if age_seconds <= ttl_seconds:
            # Convert DynamoDB items to proper format
            return {
                "total_count": int(item.get("total_count", 0)),
                "rating": str(item.get("rating", "")),
                "reviews": item.get("reviews", []),
                "created_at": created_at
            }
        return None
        
    except ClientError as e:
        logger.error(f"DynamoDB error: {e}")
        return None  # Fail open - proceed to Google API

async def save_to_dynamo(hotel_uuid: str, data: Dict[str, Any]) -> None:
    """Save reviews to DynamoDB"""
    item = {
        "hotel_uuid": hotel_uuid,
        "total_count": data["total_count"],
        "rating": data["rating"],
        "reviews": data["reviews"],
        "created_at": int(time.time())
    }
    
    try:
        table.put_item(Item=item)
        logger.info(f"Saved to DynamoDB for hotel_uuid={hotel_uuid}")
    except ClientError as e:
        logger.error(f"Failed to save to DynamoDB: {e}")
        raise

async def fetch_from_google(request: HotelReviewRequest) -> Dict[str, Any]:
    """Fetch hotel data from Google Places API"""
    
    # Step 1: Search for the place
    place_id = await search_place(request)
    if not place_id:
        raise HTTPException(
            status_code=404,
            detail=f"Hotel '{request.hotel_name}' not found in {request.city}, {request.country}"
        )
    
    # Step 2: Get place details
    details = await get_place_details(place_id)
    
    # Step 3: Normalize the data
    return normalize_place_data(details)

async def search_place(request: HotelReviewRequest) -> Optional[str]:
    """Search for a place using Google Places Text Search"""
    
    # Build search query
    query = f"{request.hotel_name} {request.city} {request.country}"
    
    payload = {
        "textQuery": query,
        "includedType": "lodging",
        "languageCode": "en",
        "maxResultCount": 1
    }
    
    # Add location bias if coordinates are provided
    if request.latitude and request.longitude:
        payload["locationBias"] = {
            "circle": {
                "center": {
                    "latitude": request.latitude,
                    "longitude": request.longitude
                },
                "radius": 500.0
            }
        }
    
    # Make API request
    result = await google_post(
        endpoint="places:searchText",
        payload=payload,
        field_mask="places.id,places.displayName"
    )
    
    places = result.get("places", [])
    if not places:
        logger.warning(f"No places found for query: {query}")
        return None
    
    place_id = places[0].get("id")
    logger.info(f"Found place_id={place_id} for {request.hotel_name}")
    return place_id

async def get_place_details(place_id: str) -> Dict[str, Any]:
    """Get detailed information about a place"""
    return await google_get(
        endpoint=f"places/{place_id}",
        field_mask="id,rating,userRatingCount,reviews"
    )

def normalize_place_data(details: Dict[str, Any]) -> Dict[str, Any]:
    """Convert Google Places API response to our format"""
    
    raw_reviews = details.get("reviews", [])
    reviews = []
    
    for rev in raw_reviews[:MAX_REVIEWS]:
        author = rev.get("authorAttribution", {})
        text_obj = rev.get("text", {}) or rev.get("originalText", {})
        
        reviews.append({
            "author_name": author.get("displayName", "Anonymous"),
            "author_url": author.get("uri", ""),
            "rating": rev.get("rating"),
            "text": text_obj.get("text", ""),
            "language": text_obj.get("languageCode", ""),
            "time": rev.get("publishTime", ""),
            "relative_time": rev.get("relativePublishTimeDescription", "")
        })
    
    return {
        "total_count": details.get("userRatingCount", 0),
        "rating": str(round(float(details.get("rating", 0.0)), 1)),
        "reviews": reviews
    }

async def google_post(endpoint: str, payload: Dict, field_mask: str) -> Dict:
    """Make a POST request to Google Places API"""
    url = f"{PLACES_BASE_URL}/{endpoint}"
    data = json.dumps(payload).encode("utf-8")
    
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Goog-FieldMask": field_mask
    }
    
    return await http_request(url, headers, data, "POST")

async def google_get(endpoint: str, field_mask: str) -> Dict:
    """Make a GET request to Google Places API"""
    url = f"{PLACES_BASE_URL}/{endpoint}"
    
    headers = {
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Goog-FieldMask": field_mask
    }
    
    return await http_request(url, headers, None, "GET")

async def http_request(url: str, headers: Dict, data: Optional[bytes], method: str) -> Dict:
    """Make an HTTP request"""
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method
    )
    
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            body = response.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        logger.error(f"Google API HTTP {e.code}: {error_body}")
        raise Exception(f"Google API error {e.code}: {error_body}")
    except urllib.error.URLError as e:
        logger.error(f"Network error: {e.reason}")
        raise Exception(f"Network error: {e.reason}")
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON from Google: {e}")
        raise Exception(f"Invalid JSON from Google: {e}")

# ── Lambda Handler ────────────────────────────────────────────────────────────
# This is what AWS Lambda actually calls
lambda_handler = Mangum(app, lifespan="off")