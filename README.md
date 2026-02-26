# fondoke-hotel-reviews

A Lambda function that fetches hotel reviews from Google Places API and caches them in DynamoDB. Built with FastAPI + Mangum so it runs both locally and on AWS Lambda without any changes.

---

## What it does

You POST a hotel's details (name, city, country, uuid) and get back the Google rating + reviews. The first call hits Google, saves the result to DynamoDB, and every call after that within 3 days just reads from the cache. When the cache is older than 3 days it goes back to Google automatically.

---

## Stack

- **FastAPI** — handles routing, request validation, and response models
- **Mangum** — wraps FastAPI so AWS Lambda can invoke it (the `lambda_handler` at the bottom)
- **Pydantic** — validates incoming request fields; if a required field is missing FastAPI returns 422 automatically
- **boto3** — connects to DynamoDB
- **urllib** (stdlib) — makes the actual HTTP calls to Google, no extra dependencies needed

---

## Environment Variables

| Variable | Default | What it does |
|---|---|---|
| `GOOGLE_API_KEY` | *(empty)* | Your Google Places API key |
| `DYNAMODB_TABLE_NAME` | `fondoke_reviews_external` | DynamoDB table to read/write |
| `DYNAMODB_REGION` | `eu-west-1` | AWS region for DynamoDB |
| `CACHE_TTL_DAYS` | `3` | How many days before a cache record is considered stale |
| `MAX_REVIEWS` | `10` | Max number of reviews to keep per hotel |
| `LOG_LEVEL` | `INFO` | Python logging level |

---

## Endpoints

### `GET /`
Health check. Returns `{"status": "healthy", "service": "fondoke-hotel-reviews"}`.

### `POST /reviews`

**Request body:**
```json
{
  "hotel_uuid": "some-internal-id",
  "hotel_name": "The Shelbourne Hotel",
  "city": "Dublin",
  "country": "Ireland",
  "latitude": 53.3381,
  "longitude": -6.2592
}
```

`latitude` and `longitude` are optional. If provided they're passed to Google as a location bias to help find the right hotel when the name is common.

**Response:**
```json
{
  "hotel_uuid": "some-internal-id",
  "total_count": 3840,
  "rating": "4.6",
  "reviews": [...],
  "source": "cache",
  "last_updated": 1706789012
}
```

`source` will be either `"cache"` or `"google"` so you know where the data came from.

---

## How the caching works

1. Request comes in with a `hotel_uuid`
2. Check DynamoDB for that `hotel_uuid`
3. If found and `created_at` is less than 3 days old → return it, done
4. If not found, or older than 3 days → call Google
5. Save the Google result to DynamoDB with current timestamp
6. Return the fresh data

If the DynamoDB write fails for some reason, the function still returns the Google data to the caller — it just won't be cached for next time. The Google fetch result is never dropped on the floor.

---

## How the Google Places flow works

There are two API calls per uncached hotel:

1. **Text Search** (`POST places:searchText`) — sends `"Hotel Name City Country"` as a text query with `includedType: lodging`. If coordinates were provided it adds a 500m `locationBias` circle. This returns the Google `place_id`.

2. **Place Details** (`GET places/{place_id}`) — fetches `rating`, `userRatingCount`, and `reviews` for that place.

The field masks on both calls keep costs down by only requesting what's actually needed.

Reviews come back from Google in their own format. The `_normalize()` function converts them:
- `authorAttribution.displayName` → `author_name`
- `authorAttribution.uri` → `author_url`
- `text.text` → `text`
- `text.languageCode` → `language`
- `publishTime` → `time`
- `relativePublishTimeDescription` → `relative_time`

Google only returns 5 reviews max from their API. `MAX_REVIEWS` env var caps this further if needed.

---

## DynamoDB table

The partition key is `hotel_uuid`. Each record stores `total_count`, `rating`, `reviews` (list), and `created_at` (Unix timestamp).

---

## Running tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

Tests use FastAPI's `TestClient` (backed by httpx) so no real server is needed. DynamoDB and Google API are both mocked with `unittest.mock.patch`.

**What's covered:**
- Health check
- Cache hit returns cached data without calling Google
- Cache miss calls Google and saves to DynamoDB
- Stale cache (older than 3 days) is ignored and Google is called
- Missing required fields return 422
- Google API failure returns 502
- DynamoDB write failure still returns the Google data
- Optional coordinates are passed through to Google correctly
- Different hotel UUIDs are stored independently
- Hotels with no reviews return an empty list without crashing
