# OPTCG API

A REST API for the One Piece Trading Card Game. Provides card and set data for all 4,347 cards across 51 sets, with filtering, pagination, and multi-set support for binder apps.

**Live API:** `https://optcg-api-rm6b.onrender.com`  
**Docs:** `https://optcg-api-rm6b.onrender.com/docs`

---

## Endpoints

### Cards
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/cards` | All cards with filters |
| GET | `/cards/{id}` | Single card by ID |

### Sets
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/sets` | All sets |
| GET | `/sets/{id}/cards` | All cards in a set |

---

## Filters (`GET /cards`)

| Parameter | Type | Example | Description |
|-----------|------|---------|-------------|
| `set_id` | string | `OP-01` | Filter by set |
| `color` | string | `Red` | Filter by color |
| `category` | string | `Leader` | Leader, Character, Event, Stage, Don |
| `rarity` | string | `SuperRare` | Common, Uncommon, Rare, SuperRare, SecretRare, Leader |
| `name` | string | `Luffy` | Partial name search |
| `parallel` | boolean | `true` | true = parallel only, false = base only |
| `min_power` | int | `5000` | Minimum power |
| `max_power` | int | `9000` | Maximum power |
| `min_cost` | int | `1` | Minimum cost |
| `max_cost` | int | `5` | Maximum cost |
| `page` | int | `1` | Page number |
| `page_size` | int | `50` | Results per page (max 200) |

---

## Example Requests

```
GET /cards?color=Red&category=Leader
GET /cards?name=Luffy&min_power=5000
GET /cards?set_id=OP-01&rarity=SecretRare
GET /cards/OP01-001
GET /sets/OP-01/cards
```

## Example Response (`GET /cards/OP01-001`)

```json
{
  "id": "OP01-001",
  "name": "Roronoa Zoro",
  "rarity": "Leader",
  "category": "Leader",
  "colors": ["Red"],
  "cost": 5,
  "power": 5000,
  "counter": null,
  "attributes": ["Slash"],
  "types": ["Supernovas", "Straw Hat Crew"],
  "effect": "[DON!! x1] [Your Turn] All of your Characters gain +1000 power.",
  "trigger": null,
  "parallel": false,
  "image_url": "https://en.onepiece-cardgame.com/images/cardlist/card/OP01-001.png",
  "sets": [{ "id": "OP-01", "label": "BOOSTER PACK -ROMANCE DAWN- [OP-01]" }]
}
```

---

## Data Coverage

- **4,346 unique cards** across **51 sets**
- Booster packs OP-01 through OP-15
- Starter decks ST-01 through ST-29
- Extra Boosters, Premium Boosters, Promos
- Parallel/alt-art cards tracked with `base_id` reference
- Cards appearing in multiple sets fully supported via junction table

---

## Tech Stack

- **Scraper:** Python + Playwright
- **Database:** PostgreSQL (Supabase)
- **API:** FastAPI + psycopg2
- **Hosting:** Render

---

## Running Locally

```bash
git clone https://github.com/arjunkai/optcg-api.git
cd optcg-api
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` file:
```
DATABASE_URL=your_supabase_connection_string
```

Start the server:
```bash
uvicorn main:app --reload
```

Open `http://localhost:8000/docs`

---

## Data Source

Card data scraped from the official [One Piece Card Game website](https://en.onepiece-cardgame.com/cardlist/).  
All card data is © Eiichiro Oda / Shueisha, Toei Animation, Bandai Namco Entertainment Inc.