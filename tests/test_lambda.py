import json
import os
import time
import unittest
from unittest.mock import MagicMock, patch
os.environ["GOOGLE_API_KEY"]      = "test-key"
os.environ["DYNAMODB_TABLE_NAME"] = "fondoke_reviews_external"
os.environ["DYNAMODB_REGION"]     = "eu-west-1"
import sys
sys.path.insert(0, "src")
import lambda_function
VALID_EVENT = {
    "hotel_uuid":  "uuid-001",
    "hotel_name":  "The Shelbourne Hotel",
    "city":        "Dublin",
    "country":     "Ireland"
}
FRESH_CACHE = {
    "hotel_uuid":  "uuid-001",
    "total_count": 3840,
    "rating":      "4.6",
    "reviews":     [{"author_name": "Alice", "rating": 5, "text": "Great!"}],
    "created_at":  int(time.time())   # fresh — just now
}
GOOGLE_DATA = {
    "total_count": 3840,
    "rating":      "4.6",
    "reviews":     [{"author_name": "Bob", "rating": 4, "text": "Lovely"}],
    "fetched_at":  int(time.time())
}
class TestLambda(unittest.TestCase):
    @patch("lambda_function.table")
    @patch("lambda_function._fetch_from_google")
    def test_cache_hit_skips_google(self, mock_google, mock_table):
        mock_table.get_item.return_value = {"Item": FRESH_CACHE}
        result = lambda_function.lambda_handler(VALID_EVENT, None)
        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["source"], "cache")
        mock_google.assert_not_called()
    @patch("lambda_function.table")
    @patch("lambda_function._fetch_from_google", return_value=GOOGLE_DATA)
    def test_cache_miss_calls_google(self, mock_google, mock_table):
        mock_table.get_item.return_value = {}   # no item
        mock_table.put_item.return_value = {}
        result = lambda_function.lambda_handler(VALID_EVENT, None)
        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["source"], "google")
        mock_google.assert_called_once()
    @patch("lambda_function.table")
    @patch("lambda_function._fetch_from_google", side_effect=Exception("timeout"))
    def test_google_error_returns_502(self, mock_google, mock_table):
        mock_table.get_item.return_value = {}
        result = lambda_function.lambda_handler(VALID_EVENT, None)
        self.assertEqual(result["statusCode"], 502)
    def test_missing_field_returns_400(self):
        for field in ["hotel_uuid", "hotel_name", "city", "country"]:
            bad_event = {k: v for k, v in VALID_EVENT.items() if k != field}
            result = lambda_function.lambda_handler(bad_event, None)
            self.assertEqual(result["statusCode"], 400)
    @patch("lambda_function.table")
    @patch("lambda_function._fetch_from_google", return_value=GOOGLE_DATA)
    def test_api_gateway_string_body(self, mock_google, mock_table):
        mock_table.get_item.return_value = {}
        mock_table.put_item.return_value = {}
        event = {"body": json.dumps(VALID_EVENT)}
        result = lambda_function.lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 200)
if __name__ == "__main__":
    unittest.main()