"""
Tests for fondoke-hotel-reviews FastAPI Lambda
"""
import sys
import os
from pathlib import Path
from fastapi import FastAPI, HTTPException

# Add the src directory to Python path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import json
import time
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

# Set environment variables BEFORE importing the app
os.environ["GOOGLE_API_KEY"] = "test-key-123"
os.environ["DYNAMODB_TABLE_NAME"] = "fondoke_reviews_external"
os.environ["DYNAMODB_REGION"] = "eu-west-1"
os.environ["CACHE_TTL_DAYS"] = "3"
os.environ["MAX_REVIEWS"] = "10"

from fastapi.testclient import TestClient
from lambda_function import app

client = TestClient(app)

# ── Test Data ─────────────────────────────────────────────────────────────────
VALID_PAYLOAD = {
    "hotel_uuid": "test-hotel-001",
    "hotel_name": "The Shelbourne Hotel",
    "city": "Dublin",
    "country": "Ireland"
}

FRESH_CACHE = {
    "hotel_uuid": "test-hotel-001",
    "total_count": 3840,
    "rating": "4.6",
    "reviews": [
        {
            "author_name": "Alice Smith",
            "author_url": "https://google.com/user/alice",
            "rating": 5,
            "text": "Absolutely wonderful hotel!",
            "language": "en",
            "time": "2026-01-15T10:00:00Z",
            "relative_time": "a month ago"
        }
    ],
    "created_at": int(time.time())
}

STALE_CACHE = {
    "hotel_uuid": "test-hotel-001",
    "total_count": 3840,
    "rating": "4.6",
    "reviews": [
        {
            "author_name": "Alice Smith",
            "author_url": "https://google.com/user/alice",
            "rating": 5,
            "text": "Absolutely wonderful hotel!",
            "language": "en",
            "time": "2026-01-15T10:00:00Z",
            "relative_time": "a month ago"
        }
    ],
    "created_at": int(time.time()) - (4 * 24 * 60 * 60)  # 4 days old
}

GOOGLE_RESPONSE = {
    "total_count": 3840,
    "rating": "4.6",
    "reviews": [
        {
            "author_name": "Bob Jones",
            "author_url": "https://google.com/user/bob",
            "rating": 4,
            "text": "Great stay, highly recommend!",
            "language": "en",
            "time": "2026-02-01T09:00:00Z",
            "relative_time": "3 weeks ago"
        }
    ]
}

# ── Tests ─────────────────────────────────────────────────────────────────────
def test_health_check():
    """Test health check endpoint"""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "fondoke-hotel-reviews"
    assert "timestamp" in data

def test_health_check_alt():
    """Test alternative health check endpoint"""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"

@pytest.mark.asyncio
@patch("lambda_function.table")
async def test_cache_hit(mock_table):
    """Test cache hit returns cached data"""
    mock_table.get_item.return_value = {"Item": FRESH_CACHE}
    
    response = client.post("/reviews", json=VALID_PAYLOAD)
    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "cache"
    assert data["total_count"] == 3840
    assert data["rating"] == "4.6"
    assert data["hotel_uuid"] == "test-hotel-001"
    assert len(data["reviews"]) == 1
    assert data["reviews"][0]["author_name"] == "Alice Smith"

@pytest.mark.asyncio
@patch("lambda_function.table")
@patch("lambda_function.fetch_from_google", new_callable=AsyncMock)
async def test_cache_miss(mock_google, mock_table):
    """Test cache miss calls Google and returns data"""
    mock_table.get_item.return_value = {}
    mock_google.return_value = GOOGLE_RESPONSE
    
    response = client.post("/reviews", json=VALID_PAYLOAD)
    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "google"
    assert data["total_count"] == 3840
    assert data["rating"] == "4.6"
    assert len(data["reviews"]) == 1
    assert data["reviews"][0]["author_name"] == "Bob Jones"

@pytest.mark.asyncio
@patch("lambda_function.table")
async def test_stale_cache(mock_table):
    """Test stale cache (older than 3 days)"""
    mock_table.get_item.return_value = {"Item": STALE_CACHE}
    
    # Mock the Google fetch to avoid actual API call
    with patch("lambda_function.fetch_from_google", new_callable=AsyncMock) as mock_google:
        mock_google.return_value = GOOGLE_RESPONSE
        response = client.post("/reviews", json=VALID_PAYLOAD)
        
        assert response.status_code == 200
        data = response.json()
        assert data["source"] == "google"  # Should fetch from Google
        mock_google.assert_called_once()

def test_missing_required_fields():
    """Test validation of required fields"""
    # Missing hotel_uuid
    payload = VALID_PAYLOAD.copy()
    del payload["hotel_uuid"]
    
    response = client.post("/reviews", json=payload)
    assert response.status_code == 422
    
    # Missing hotel_name
    payload = VALID_PAYLOAD.copy()
    del payload["hotel_name"]
    
    response = client.post("/reviews", json=payload)
    assert response.status_code == 422
    
    # Missing city
    payload = VALID_PAYLOAD.copy()
    del payload["city"]
    
    response = client.post("/reviews", json=payload)
    assert response.status_code == 422
    
    # Missing country
    payload = VALID_PAYLOAD.copy()
    del payload["country"]
    
    response = client.post("/reviews", json=payload)
    assert response.status_code == 422

def test_coordinate_validation():
    """Test coordinate validation"""
    # Test missing one coordinate
    payload = VALID_PAYLOAD.copy()
    payload["latitude"] = 53.3381
    # Missing longitude
    
    response = client.post("/reviews", json=payload)
    assert response.status_code == 422
    error_detail = response.json()["detail"][0]["msg"]
    assert "Both latitude and longitude must be provided together" in error_detail
    
    # Test invalid latitude
    payload = VALID_PAYLOAD.copy()
    payload["latitude"] = 100  # Invalid (>90)
    payload["longitude"] = -6.2592
    
    response = client.post("/reviews", json=payload)
    assert response.status_code == 422
    
    # Test invalid longitude
    payload = VALID_PAYLOAD.copy()
    payload["latitude"] = 53.3381
    payload["longitude"] = 200  # Invalid (>180)
    
    response = client.post("/reviews", json=payload)
    assert response.status_code == 422
    
    # Test valid coordinates
    payload = VALID_PAYLOAD.copy()
    payload["latitude"] = 53.3381
    payload["longitude"] = -6.2592
    
    with patch("lambda_function.table") as mock_table:
        mock_table.get_item.return_value = {}
        with patch("lambda_function.fetch_from_google", new_callable=AsyncMock) as mock_google:
            mock_google.return_value = GOOGLE_RESPONSE
            response = client.post("/reviews", json=payload)
            assert response.status_code == 200

@pytest.mark.asyncio
@patch("lambda_function.table")
@patch("lambda_function.fetch_from_google", new_callable=AsyncMock)
async def test_google_api_failure(mock_google, mock_table):
    """Test handling of Google API failure"""
    mock_table.get_item.return_value = {}
    mock_google.side_effect = Exception("Google API error")
    
    response = client.post("/reviews", json=VALID_PAYLOAD)
    assert response.status_code == 502  # Bad Gateway

@pytest.mark.asyncio
@patch("lambda_function.table")
@patch("lambda_function.fetch_from_google", new_callable=AsyncMock)
async def test_dynamodb_save_failure(mock_google, mock_table):
    """Test that DynamoDB save failure still returns data"""
    mock_table.get_item.return_value = {}
    mock_google.return_value = GOOGLE_RESPONSE
    mock_table.put_item.side_effect = Exception("DynamoDB error")
    
    response = client.post("/reviews", json=VALID_PAYLOAD)
    assert response.status_code == 200  # Still returns data
    data = response.json()
    assert data["source"] == "google"
    assert data["total_count"] == 3840

@pytest.mark.asyncio
@patch("lambda_function.table")
async def test_dynamodb_get_failure(mock_table):
    """Test DynamoDB get failure (should fail open to Google)"""
    mock_table.get_item.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError"}}, "GetItem"
    )
    
    with patch("lambda_function.fetch_from_google", new_callable=AsyncMock) as mock_google:
        mock_google.return_value = GOOGLE_RESPONSE
        response = client.post("/reviews", json=VALID_PAYLOAD)
        
        assert response.status_code == 200
        data = response.json()
        assert data["source"] == "google"
        mock_google.assert_called_once()

@pytest.mark.asyncio
@patch("lambda_function.table")
async def test_hotel_not_found_in_google(mock_table):
    """Test when hotel is not found in Google Places"""
    mock_table.get_item.return_value = {}
    
    with patch("lambda_function.fetch_from_google", new_callable=AsyncMock) as mock_google:
        mock_google.side_effect = HTTPException(
            status_code=404, 
            detail="Hotel not found"
        )
        
        response = client.post("/reviews", json=VALID_PAYLOAD)
        assert response.status_code == 404