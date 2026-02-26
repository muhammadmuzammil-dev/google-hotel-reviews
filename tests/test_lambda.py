"""
Tests for fondoke-hotel-reviews FastAPI Lambda
We use FastAPI's TestClient which simulates real HTTP requests
without actually starting a server. Much faster than real HTTP calls.
TestClient uses httpx under the hood (that's why we install httpx).
"""
import json
import os
import time
import pytest
from unittest.mock import patch, MagicMock
os.environ["GOOGLE_API_KEY"]      = "test-key-123"
os.environ["DYNAMODB_TABLE_NAME"] = "fondoke_reviews_external"
os.environ["DYNAMODB_REGION"]     = "eu-west-1"
os.environ["CACHE_TTL_DAYS"]      = "3"
os.environ["MAX_REVIEWS"]         = "10"
import sys
sys.path.insert(0, "src") 
from fastapi.testclient import TestClient  
from lambda_function import app           

client = TestClient(app)

VALID_PAYLOAD = {
    "hotel_uuid":  "test-uuid-001",
    "hotel_name":  "The Shelbourne Hotel",
    "city":        "Dublin",
    "country":     "Ireland"
}
FRESH_CACHE = {
    "hotel_uuid":  "test-uuid-001",
    "total_count": 3840,
    "rating":      "4.6",
    "reviews": [
        {
            "author_name":   "Alice Smith",
            "author_url":    "https://google.com/user/alice",
            "rating":        5,
            "text":          "Absolutely wonderful hotel!",
            "language":      "en",
            "time":          "2026-01-15T10:00:00Z",
            "relative_time": "a month ago"
        }
    ],
    "created_at": int(time.time()) 
}

GOOGLE_RESPONSE = {
    "total_count": 3840,
    "rating":      "4.6",
    "reviews": [
        {
            "author_name":   "Bob Jones",
            "author_url":    "https://google.com/user/bob",
            "rating":        4,
            "text":          "Great stay, highly recommend!",
            "language":      "en",
            "time":          "2026-02-01T09:00:00Z",
            "relative_time": "3 weeks ago"
        }
    ],
    "fetched_at": int(time.time())
}


def test_health_check():
    """
    Test the GET / endpoint.
    Should return 200 with status: healthy.
    """
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert response.json()["service"] == "fondoke-hotel-reviews"


@patch("lambda_function.table")
def test_cache_hit_returns_cached_data(mock_table):
    """
    When DynamoDB has a FRESH record:
    - Should return 200
    - source should be "cache"
    - Google API should NOT be called
    """
    mock_table.get_item.return_value = {"Item": FRESH_CACHE}
    response = client.post("/reviews", json=VALID_PAYLOAD)
    assert response.status_code == 200
    data = response.json()
    assert data["source"]       == "cache"        
    assert data["total_count"]  == 3840
    assert data["rating"]       == "4.6"
    assert data["hotel_uuid"]   == "test-uuid-001"
    assert isinstance(data["reviews"], list)      
    mock_table.get_item.assert_called_once_with(
        Key={"hotel_uuid": "test-uuid-001"}
    )

@patch("lambda_function.table")
@patch("lambda_function._fetch_from_google", return_value=GOOGLE_RESPONSE)
def test_cache_miss_calls_google_and_saves(mock_google, mock_table):
    """
    When DynamoDB has NO record:
    - Should call Google API
    - Should save result to DynamoDB
    - Should return 200 with source="google"
    """
    mock_table.get_item.return_value = {}
    mock_table.put_item.return_value = {}
    response = client.post("/reviews", json=VALID_PAYLOAD)
    assert response.status_code == 200
    data = response.json()
    assert data["source"]      == "google"    
    assert data["total_count"] == 3840
    assert data["rating"]      == "4.6"
    mock_google.assert_called_once()
    mock_table.put_item.assert_called_once()


@patch("lambda_function.table")
@patch("lambda_function._fetch_from_google", return_value=GOOGLE_RESPONSE)
def test_stale_cache_calls_google(mock_google, mock_table):
    """
    When DynamoDB has a record but it is OLDER than 3 days:
    - Should ignore the stale cache
    - Should call Google for fresh data
    """
    stale_record = dict(FRESH_CACHE)
    stale_record["created_at"] = int(time.time()) - (4 * 86400)
    mock_table.get_item.return_value = {"Item": stale_record}
    mock_table.put_item.return_value = {}
    response = client.post("/reviews", json=VALID_PAYLOAD)
    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "google"   
    mock_google.assert_called_once()


def test_missing_hotel_uuid_returns_422():
    """
    hotel_uuid is required. If missing, FastAPI/Pydantic returns 422.
    422 = Unprocessable Entity (validation error)
    """
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "hotel_uuid"}
    response = client.post("/reviews", json=payload)
    assert response.status_code == 422
def test_missing_hotel_name_returns_422():
    """hotel_name is required."""
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "hotel_name"}
    response = client.post("/reviews", json=payload)
    assert response.status_code == 422
def test_missing_city_returns_422():
    """city is required."""
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "city"}
    response = client.post("/reviews", json=payload)
    assert response.status_code == 422
def test_missing_country_returns_422():
    """country is required."""
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "country"}
    response = client.post("/reviews", json=payload)
    assert response.status_code == 422


@patch("lambda_function.table")
@patch("lambda_function._fetch_from_google", side_effect=Exception("Connection timeout"))
def test_google_api_failure_returns_502(mock_google, mock_table):
    """
    When Google API fails:
    - Should return 502 Bad Gateway
    - Error message should be in response
    """
    mock_table.get_item.return_value = {}
    response = client.post("/reviews", json=VALID_PAYLOAD)
    assert response.status_code == 502
    assert "error" in response.json()


@patch("lambda_function.table")
@patch("lambda_function._fetch_from_google", return_value=GOOGLE_RESPONSE)
def test_dynamo_write_fail_still_returns_google_data(mock_google, mock_table):
    """
    Even if DynamoDB write fails:
    - Should still return Google data (don't lose the response)
    - source should be "google"
    """
    mock_table.get_item.return_value = {}
    mock_table.put_item.side_effect = Exception("DynamoDB unavailable")
    response = client.post("/reviews", json=VALID_PAYLOAD)
    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "google"


@patch("lambda_function.table")
@patch("lambda_function._fetch_from_google", return_value=GOOGLE_RESPONSE)
def test_with_coordinates(mock_google, mock_table):
    """
    Optional latitude/longitude should be accepted and passed to Google.
    """
    mock_table.get_item.return_value = {}
    mock_table.put_item.return_value = {}
    payload_with_coords = {
        **VALID_PAYLOAD,
        "latitude":  53.3381,
        "longitude": -6.2592
    }
    response = client.post("/reviews", json=payload_with_coords)
    assert response.status_code == 200
    call_args = mock_google.call_args
    assert call_args.kwargs["latitude"]  == 53.3381
    assert call_args.kwargs["longitude"] == -6.2592


@patch("lambda_function.table")
@patch("lambda_function._fetch_from_google", return_value=GOOGLE_RESPONSE)
def test_different_hotel_uuid_stored_separately(mock_google, mock_table):
    """
    Each hotel_uuid should be stored separately in DynamoDB.
    Calling with a different hotel should save with that hotel's UUID.
    """
    mock_table.get_item.return_value = {}
    mock_table.put_item.return_value = {}
    payload = {
        "hotel_uuid":  "ritz-london-001",
        "hotel_name":  "The Ritz",
        "city":        "London",
        "country":     "England"
    }
    response = client.post("/reviews", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["hotel_uuid"] == "ritz-london-001"  


@patch("lambda_function.table")
@patch("lambda_function._fetch_from_google")
def test_handles_empty_reviews(mock_google, mock_table):
    """
    If Google returns no reviews (new hotel):
    - Should still return 200
    - reviews should be an empty list, not crash
    """
    mock_google.return_value = {
        "total_count": 5,
        "rating":      "4.0",
        "reviews":     [],  
        "fetched_at":  int(time.time())
    }
    mock_table.get_item.return_value = {}
    mock_table.put_item.return_value = {}
    response = client.post("/reviews", json=VALID_PAYLOAD)
    assert response.status_code == 200
    assert response.json()["reviews"] == []