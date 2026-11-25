import asyncio
import json
import os
import re
import sqlite3
import traceback
from pathlib import Path
from typing import Any
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ROOT_DIR / "platina.db"


def load_env_file() -> None:
    env_path = ROOT_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()

STEAM_API_KEY = os.getenv("STEAM_API_KEY", "").strip()
STEAM_BASE = "https://api.steampowered.com"


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                tg_id INTEGER PRIMARY KEY,
                steam_id TEXT NOT NULL,
                persona_name TEXT NOT NULL,
                avatar TEXT NOT NULL,
                stats_json TEXT NOT NULL,
                badges_json TEXT NOT NULL,
                all_games_json TEXT NOT NULL,
                completed_100_games_json TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def compute_badges(stats: dict[str, Any]) -> list[dict[str, Any]]:
    c100 = stats["completed_100_count"]
    total_games = stats["total_games_count"]
    hours = stats["total_hours"]
    ach = stats["total_achievements"]

    return [
        {
            "id": "pioneer",
            "title": "Первопроходец",
            "desc": "Привязал профиль Steam к TrophyRoom",
            "unlocked": True,
            "tier": "common",
        },
        {
            "id": "first_100",
            "title": "Первая платина",
            "desc": "Закрыл хотя бы 1 игру на 100%",
            "unlocked": c100 >= 1,
            "tier": "gold",
        },
        {
            "id": "hunter_5",
            "title": "Охотник за трофеями",
            "desc": "Закрыл 5 игр на 100%",
            "unlocked": c100 >= 5,
            "tier": "gold",
        },
        {
            "id": "master_15",
            "title": "Мастер зачистки",
            "desc": "Закрыл 15 игр на 100%",
            "unlocked": c100 >= 15,
            "tier": "platinum",
        },
        {
            "id": "collector_50",
            "title": "Коллекционер",
            "desc": "50 и более игр в библиотеке Steam",
            "unlocked": total_games >= 50,
            "tier": "common",
        },
        {
            "id": "veteran_500",
            "title": "Ветеран",
            "desc": "Наиграл более 500 часов суммарно",
            "unlocked": hours >= 500,
            "tier": "common",
        },
        {
            "id": "ach_1000",
            "title": "Тысячник",
            "desc": "Выбил 1 000 достижений в играх",
            "unlocked": ach >= 1000,
            "tier": "platinum",
        },
    ]


def save_profile_to_db(profile: dict[str, Any]) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO profiles (
                tg_id, steam_id, persona_name, avatar,
                stats_json, badges_json, all_games_json, completed_100_games_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(tg_id) DO UPDATE SET
                steam_id = excluded.steam_id,
                persona_name = excluded.persona_name,
                avatar = excluded.avatar,
                stats_json = excluded.stats_json,
                badges_json = excluded.badges_json,
                all_games_json = excluded.all_games_json,
                completed_100_games_json = excluded.completed_100_games_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                profile["tg_id"],
                profile["steam_id"],
                profile["persona_name"],
                profile["avatar"],
                json.dumps(profile["stats"], ensure_ascii=False),
                json.dumps(profile["badges"], ensure_ascii=False),
                json.dumps(profile["all_games"], ensure_ascii=False),
                json.dumps(profile["completed_100_games"], ensure_ascii=False),
            ),
        )
        conn.commit()


def load_profile_from_db(tg_id: int) -> dict[str, Any] | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM profiles WHERE tg_id = ?", (tg_id,)).fetchone()
        if not row:
            return None
        return {
            "tg_id": row["tg_id"],
            "steam_id": row["steam_id"],
            "persona_name": row["persona_name"],
            "avatar": row["avatar"],
            "stats": json.loads(row["stats_json"]),
            "badges": json.loads(row["badges_json"]),
            "all_games": json.loads(row["all_games_json"]),
            "completed_100_games": json.loads(row["completed_100_games_json"]),
            "updated_at": row["updated_at"],
        }


init_db()

app = FastAPI(title="TrophyRoom Mini App")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class LinkSteamRequest(BaseModel):
    tg_id: int
    steam_input: str


async def resolve_steam_id64(client: httpx.AsyncClient, raw: str) -> str:
    value = raw.strip()
    if re.fullmatch(r"\d{17}", value):
        return value

    m_profile = re.search(r"steamcommunity\.com/profiles/(\d{17})", value)
    if m_profile:
        return m_profile.group(1)

    m_vanity = re.search(r"steamcommunity\.com/id/([^/?#]+)", value)
    vanity = m_vanity.group(1) if m_vanity else value

    resp = await client.get(
        f"{STEAM_BASE}/ISteamUser/ResolveVanityURL/v1/",
        params={"key": STEAM_API_KEY, "vanityurl": vanity},
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json().get("response", {})
    if data.get("success") == 1 and data.get("steamid"):
        return str(data["steamid"])

    raise HTTPException(
        status_code=400,
        detail="Не удалось определить SteamID64. Проверьте ссылку, ID или открытость профиля.",
    )


async def fetch_game_100_status(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    steam_id: str,
    game: dict[str, Any],
) -> dict[str, Any]:
    appid = game["appid"]
    name = game.get("name", f"App {appid}")
    playtime_hours = round(game.get("playtime_forever", 0) / 60, 1)
    icon_hash = game.get("img_icon_url", "")
    icon_url = (
        f"https://media.steampowered.com/steamcommunity/public/images/apps/{appid}/{icon_hash}.jpg"
        if icon_hash
        else f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"
    )

    base_item = {
        "appid": appid,
        "name": name,
        "playtime_hours": playtime_hours,
        "icon_url": icon_url,
        "is_100": False,
        "unlocked": 0,
        "total": 0,
    }

    if game.get("playtime_forever", 0) <= 0:
        return base_item

    async with sem:
        try:
            resp = await client.get(
                f"{STEAM_BASE}/ISteamUserStats/GetPlayerAchievements/v1/",
                params={"key": STEAM_API_KEY, "steamid": steam_id, "appid": appid},
                timeout=8.0,
            )
            if resp.status_code != 200:
                return base_item
            playerstats = resp.json().get("playerstats", {})
            achievements = playerstats.get("achievements")
            if not achievements:
                return base_item

            total = len(achievements)
            unlocked = sum(1 for a in achievements if a.get("achieved") == 1)
            return {
                **base_item,
                "is_100": total > 0 and unlocked == total,
                "unlocked": unlocked,
                "total": total,
            }
        except Exception:
            return base_item


async def sync_steam_profile(tg_id: int, steam_input: str) -> dict[str, Any]:
    if not STEAM_API_KEY:
        raise HTTPException(status_code=500, detail="Не задан STEAM_API_KEY в переменных окружения")

    async with httpx.AsyncClient() as client:
        steam_id = await resolve_steam_id64(client, steam_input)

        summary_resp = await client.get(
            f"{STEAM_BASE}/ISteamUser/GetPlayerSummaries/v2/",
            params={"key": STEAM_API_KEY, "steamids": steam_id},
            timeout=10.0,
        )
        summary_resp.raise_for_status()
        players = summary_resp.json().get("response", {}).get("players", [])
        if not players:
            raise HTTPException(status_code=404, detail="Профиль Steam не найден")
        player = players[0]

        owned_resp = await client.get(
            f"{STEAM_BASE}/IPlayerService/GetOwnedGames/v1/",
            params={
                "key": STEAM_API_KEY,
                "steamid": steam_id,
                "include_appinfo": 1,
                "include_played_free_games": 1,
            },
            timeout=15.0,
        )
        owned_resp.raise_for_status()
        games_raw = owned_resp.json().get("response", {}).get("games", [])
        if not games_raw:
            raise HTTPException(
                status_code=400,
                detail="Библиотека пуста или закрыта в настройках приватности Steam.",
            )

        sem = asyncio.Semaphore(10)
        tasks = [fetch_game_100_status(client, sem, steam_id, g) for g in games_raw]
        games_processed = await asyncio.gather(*tasks)

    games_processed.sort(key=lambda x: (not x["is_100"], -x["playtime_hours"]))
    games_100 = [g for g in games_processed if g["is_100"]]

    total_games_count = len(games_processed)
    completed_100_count = len(games_100)
    total_hours = round(sum(g["playtime_hours"] for g in games_processed), 1)
    total_achievements = sum(g["unlocked"] for g in games_processed)
    games_with_ach = sum(1 for g in games_processed if g["total"] > 0)
    completion_rate = round((completed_100_count / games_with_ach) * 100, 1) if games_with_ach else 0.0

    stats = {
        "total_games_count": total_games_count,
        "completed_100_count": completed_100_count,
        "total_hours": total_hours,
        "total_achievements": total_achievements,
        "completion_rate": completion_rate,
    }

    profile_data = {
        "tg_id": tg_id,
        "steam_id": steam_id,
        "persona_name": player.get("personaname", "Player"),
        "avatar": player.get("avatarfull", ""),
        "stats": stats,
        "badges": compute_badges(stats),
        "all_games": games_processed,
        "completed_100_games": games_100,
    }
    save_profile_to_db(profile_data)
    return load_profile_from_db(tg_id) or profile_data


@app.post("/api/link-steam")
async def link_steam(payload: LinkSteamRequest):
    try:
        return await sync_steam_profile(payload.tg_id, payload.steam_input)
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Ошибка сервера: {type(e).__name__}: {e}")


@app.post("/api/sync/{tg_id}")
async def refresh_profile(tg_id: int):
    existing = load_profile_from_db(tg_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Сначала привяжите профиль Steam")
    try:
        return await sync_steam_profile(tg_id, existing["steam_id"])
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Ошибка обновления: {type(e).__name__}: {e}")


@app.get("/api/profile/{tg_id}")
async def get_profile(tg_id: int):
    user = load_profile_from_db(tg_id)
    if not user:
        raise HTTPException(status_code=404, detail="Профиль не привязан")
    return user


@app.get("/")
async def serve_index():
    return FileResponse(ROOT_DIR / "frontend" / "index.html")