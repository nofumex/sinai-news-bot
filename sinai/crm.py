import asyncio
import os
import re
import tempfile
from pathlib import Path
from dotenv import set_key
from .db import dumps, stamp
from .http import HTTP, APIError, AmbiguousSend
from .matching import contact_phones


class CRM:
    def __init__(self, config, db, http=None):
        self.config, self.db = config, db
        self.http = http or HTTP("amoCRM", interval=0.3)
        self.access = config.amocrm_access_token.get_secret_value()
        self.refresh = config.amocrm_refresh_token.get_secret_value()
        self.refresh_lock = asyncio.Lock()

    async def request(self, method, path, *, params=None, json=None, safe=True):
        # Write allowlist: contacts/leads can never be created or modified.
        if method != "GET" and not (method == "POST" and re.fullmatch(r"/api/v4/leads/[1-9]\d*/notes", path)):
            raise ValueError("CRM write prohibited")
        used_access = self.access
        try:
            return await self.http.request(method, self.config.amocrm_base_url.rstrip("/") + path,
                                           headers={"Authorization": "Bearer " + self.access},
                                           params=params, json=json, safe=safe)
        except APIError as exc:
            if exc.status != 401:
                raise
        async with self.refresh_lock:
            if self.access == used_access:
                await self.refresh_tokens()
        return await self.http.request(method, self.config.amocrm_base_url.rstrip("/") + path,
                                       headers={"Authorization": "Bearer " + self.access},
                                       params=params, json=json, safe=safe)

    async def refresh_tokens(self):
        if not self.refresh or not self.config.amocrm_client_id or not self.config.amocrm_redirect_uri:
            raise APIError("amoCRM", 401, "refresh_credentials_missing")
        data = await self.http.request("POST", self.config.amocrm_base_url.rstrip("/") + "/oauth2/access_token",
                                       safe=False, json={"client_id": self.config.amocrm_client_id,
                                       "client_secret": self.config.amocrm_client_secret.get_secret_value(),
                                       "grant_type": "refresh_token", "refresh_token": self.refresh,
                                       "redirect_uri": self.config.amocrm_redirect_uri})
        # Persist both rotated credentials with one atomic replacement before using them.
        path = self.config.env_path.resolve()
        fd, temp = tempfile.mkstemp(prefix=".env-refresh-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(path.read_text(encoding="utf-8") if path.exists() else "")
            os.chmod(temp, 0o600)
            set_key(temp, "AMOCRM_ACCESS_TOKEN", data["access_token"])
            set_key(temp, "AMOCRM_REFRESH_TOKEN", data["refresh_token"])
            with open(temp, "rb+") as file:
                os.fsync(file.fileno())
            os.replace(temp, path)
        finally:
            Path(temp).unlink(missing_ok=True)
        self.access, self.refresh = data["access_token"], data["refresh_token"]

    async def collection(self, path, key, params=None):
        for page in range(1, self.config.max_contact_pages + 1):
            data = await self.request("GET", path, params={**(params or {}), "page": page, "limit": 250})
            rows = data.get("_embedded", {}).get(key, [])
            for row in rows:
                yield row
            if not data.get("_links", {}).get("next"):
                return
        raise APIError("amoCRM", 0, "pagination_limit_incomplete_results")

    async def verify_pipeline(self):
        data = await self.request("GET", f"/api/v4/leads/pipelines/{self.config.completed_pipeline_id}")
        if data.get("id") != self.config.completed_pipeline_id:
            raise APIError("amoCRM", 0, "pipeline_mismatch")
        normalized = " ".join(data.get("name", "").casefold().replace("ё", "е").split())
        if normalized not in {"успешно завершено", "успешно завершенные"} or data.get("is_archive"):
            raise APIError("amoCRM", 0, "completed_pipeline_name_mismatch_or_archived")
        await self.db.set_setting("verified_pipeline", dumps({"id": data["id"], "name": data["name"], "at": stamp()}))
        return data

    async def contact(self, contact_id):
        return await self.request("GET", f"/api/v4/contacts/{int(contact_id)}", params={"with": "leads"})

    async def lead(self, lead_id):
        return await self.request("GET", f"/api/v4/leads/{int(lead_id)}", params={"with": "contacts"})

    async def leads_for(self, contact):
        result = []
        for item in contact.get("_embedded", {}).get("leads", []):
            result.append(await self.lead(item["id"]))
        return result

    async def by_phone(self, normalized):
        contacts = {}
        variants = {normalized, normalized.lstrip("+")}
        if normalized.startswith("+7") and len(normalized) == 12:
            variants.update({"8" + normalized[2:], normalized[2:]})
        for query in sorted(variants):
            async for contact in self.collection("/api/v4/contacts", "contacts", {"query": query}):
                if normalized in contact_phones(contact):
                    contacts[contact["id"]] = contact
        return list(contacts.values())

    async def write_note(self, key):
        note = await self.db.one("SELECT * FROM crm_notes WHERE key=?", (key,))
        if not note or note["state"] == "sent":
            return
        if self.config.test_mode or not self.config.crm_notes_enabled:
            await self.db.execute("UPDATE crm_notes SET state='test' WHERE key=?", (key,))
            return
        marker = f"[sinai:{key}]"
        async for item in self.collection(f"/api/v4/leads/{note['lead_id']}/notes", "notes"):
            if marker in item.get("params", {}).get("text", ""):
                await self.db.execute("UPDATE crm_notes SET state='sent',external_id=? WHERE key=?", (item["id"], key))
                return
        if note["state"] == "unknown":
            raise APIError("amoCRM", 0, "note_outcome_unknown_manual_check_required")
        response = await self.request("POST", f"/api/v4/leads/{note['lead_id']}/notes", safe=False,
                                      json=[{"note_type": "common", "params": {"text": note["text"] + "\n" + marker}}])
        note_id = response.get("_embedded", {}).get("notes", [{}])[0].get("id")
        if not note_id:
            raise AmbiguousSend("amoCRM", 200, "note_id_missing")
        await self.db.execute("UPDATE crm_notes SET state='sent',external_id=? WHERE key=?", (note_id, key))
