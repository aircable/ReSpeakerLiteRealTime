import logging

import httpx

from .config import Settings
from .context import PLAN_SCHEMA, extract_response_json, planner_input
from .db import Database

logger = logging.getLogger(__name__)


class Planner:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    async def update_after_session(self, project_id: int, session_id: int) -> bool:
        if not self.settings.openai_api_key:
            logger.warning("OPENAI_API_KEY is unset; plan update skipped")
            return False
        project = self.db.get_project(project_id)
        turns = self.db.session_turns(session_id)
        if not turns:
            return False
        payload = {
            "model": self.settings.planner_model,
            "reasoning": {"effort": "low"},
            "input": planner_input(project, turns),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "project_update",
                    "strict": True,
                    "schema": PLAN_SCHEMA,
                }
            },
        }
        try:
            if self.settings.openai_trace:
                logger.info(
                    "OpenAI trace planner_request model=%s session=%d turns=%d",
                    self.settings.planner_model,
                    session_id,
                    len(turns),
                )
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    "https://api.openai.com/v1/responses",
                    headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
                    json=payload,
                )
                response.raise_for_status()
            result = response.json()
            if self.settings.openai_trace:
                logger.info(
                    "OpenAI trace planner_response model=%s session=%d response_id=%s status=%s usage=%s",
                    self.settings.planner_model,
                    session_id,
                    result.get("id"),
                    result.get("status"),
                    result.get("usage", {}),
                )
            update = extract_response_json(result)
            self.db.apply_plan_update(project_id, session_id, update)
            return True
        except Exception:
            logger.exception("post-session planning failed; transcript remains intact")
            return False
