"""Recipient configuration API; raw overrides stay separate from runtime views."""

# version: 0.6.0

from copy import deepcopy
import re
import sqlite3

from flask import Blueprint, jsonify
from newspipeline import config
from newspipeline.recipients import effective_interests, merge, resolve_recipients
from newspipeline.scheduler import is_paused


def register_recipient_api(app, validate_interest, err, json_body):
    api = Blueprint("recipients", __name__)

    def sparse_interest(data, defaults=False):
        if not isinstance(data, dict):
            raise ValueError("interest configuration must be an object")
        if defaults and "name" in data:
            raise ValueError("interest_defaults cannot have a name")
        validated, error = validate_interest(data, creating=not defaults)
        if error:
            raise ValueError(error)
        return {k: validated[k] for k in data}

    def validate_recipient(data, *, shared=False):
        if not isinstance(data, dict):
            raise ValueError("recipient configuration must be an object")
        allowed = {"email", "interest_defaults", "interests"}
        if not shared:
            allowed |= {"id", "parent"}
        if set(data) - allowed:
            raise ValueError("unknown recipient configuration field")
        out = deepcopy(data)
        for key in ("id", "parent", "email"):
            if key in out:
                if not isinstance(out[key], str):
                    raise ValueError(f"{key} must be a string")
                out[key] = out[key].strip()
        if not shared and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", out.get("id", "")):
            raise ValueError("recipient id must contain 1–64 letters, numbers, underscores or dashes")
        if "interest_defaults" in out:
            out["interest_defaults"] = sparse_interest(out["interest_defaults"], defaults=True)
        if "interests" in out:
            if not isinstance(out["interests"], list):
                raise ValueError("interests must be a list")
            out["interests"] = [sparse_interest(i) for i in out["interests"]]
        return out

    def check(settings):
        resolve_recipients(settings)
        for interest in effective_interests(settings):
            # Legacy interests are validated by their existing routes.
            if interest.get("recipient_id"):
                _, error = validate_interest({k: v for k, v in interest.items() if k not in ("recipient_id", "recipient_email")}, creating=True)
                if error:
                    raise ValueError(error)

    def raw_recipient(settings, rid):
        return next((r for r in settings.get("recipients", []) if r["id"] == rid), None)

    def find(settings, rid, name):
        return next((i for i in effective_interests(settings, rid) if i["name"] == name), None)

    def view(settings, rid):
        raw = raw_recipient(settings, rid)
        if raw is None:
            return err("Recipient not found", 404)
        return jsonify({"recipient": raw, "effective": next(r for r in resolve_recipients(settings) if r["id"] == rid),
                        "interests": [{**i, "paused": is_paused(i)} for i in effective_interests(settings, rid)]})

    def save_override(raw, name, patch):
        local = next((i for i in raw.get("interests", []) if i["name"] == name), None)
        if local is None:
            local = {"name": name}
            raw.setdefault("interests", []).append(local)
        local.update(merge(local, patch))
        return local

    @api.get("/api/recipient-defaults")
    def get_defaults():
        return jsonify(config.load_settings().get("recipient_defaults", {}))

    @api.put("/api/recipient-defaults")
    def put_defaults():
        try:
            data = validate_recipient(json_body(), shared=True)
            with config.mutation_lock:
                settings = config.load_settings()
                # Complete replacement lets users remove defaults deliberately.
                settings["recipient_defaults"] = data
                check(settings)
                config.save_settings(settings)
            return jsonify(data)
        except ValueError as exc:
            return err(str(exc), 400)

    @api.get("/api/recipients")
    def list_recipients():
        with config.mutation_lock:
            settings = config.load_settings()
        return jsonify({"recipients": settings.get("recipients", []), "effective": resolve_recipients(settings)})

    @api.post("/api/recipients")
    def create_recipient():
        try:
            data = validate_recipient(json_body())
            with config.mutation_lock:
                settings = config.load_settings()
                if raw_recipient(settings, data["id"]) is not None:
                    return err("Recipient id already exists", 409)
                settings.setdefault("recipients", []).append(data)
                check(settings)
                config.save_settings(settings)
            return view(settings, data["id"]), 201
        except ValueError as exc:
            return err(str(exc), 400)

    @api.get("/api/recipients/<rid>")
    def get_recipient(rid):
        return view(config.load_settings(), rid)

    @api.put("/api/recipients/<rid>")
    def update_recipient(rid):
        try:
            body = json_body()
            if body is None:
                return err("request body must be a JSON object", 400)
            if body.get("id", rid) != rid:
                return err("recipient ids cannot be changed", 400)
            # Full replacement of the raw configuration preserves inheritance;
            # omitted fields revert to parent/default rather than being frozen.
            data = validate_recipient({**body, "id": rid})
            with config.mutation_lock:
                settings = config.load_settings()
                old = raw_recipient(settings, rid)
                if old is None:
                    return err("Recipient not found", 404)
                settings["recipients"][settings["recipients"].index(old)] = data
                check(settings)
                config.save_settings(settings)
            return view(settings, rid)
        except ValueError as exc:
            return err(str(exc), 400)

    @api.delete("/api/recipients/<rid>")
    def delete_recipient(rid):
        with config.mutation_lock:
            settings = config.load_settings()
            old = raw_recipient(settings, rid)
            if old is None:
                return err("Recipient not found", 404)
            if any(r.get("parent") == rid for r in settings["recipients"]):
                return err("Recipient is a parent; reparent its children first", 409)
            settings["recipients"].remove(old)
            config.save_settings(settings)
        return jsonify({"deleted": rid})

    @api.get("/api/recipients/<rid>/interests")
    def list_interests(rid):
        settings = config.load_settings()
        if raw_recipient(settings, rid) is None:
            return err("Recipient not found", 404)
        return jsonify({"interests": [{**i, "paused": is_paused(i)} for i in effective_interests(settings, rid)]})

    @api.post("/api/recipients/<rid>/interests")
    def create_interest(rid):
        try:
            patch = sparse_interest(json_body())
            with config.mutation_lock:
                settings = config.load_settings()
                raw = raw_recipient(settings, rid)
                if raw is None:
                    return err("Recipient not found", 404)
                if find(settings, rid, patch["name"]) is not None:
                    return err("Interest already exists for this recipient", 409)
                save_override(raw, patch["name"], {**patch, "enabled": True})
                check(settings)
                config.save_settings(settings)
            return jsonify({"interest": find(settings, rid, patch["name"])}), 201
        except ValueError as exc:
            return err(str(exc), 400)

    @api.get("/api/recipients/<rid>/interests/<name>")
    def get_interest(rid, name):
        settings = config.load_settings()
        interest = find(settings, rid, name)
        if interest is None:
            return err("Interest not found", 404)
        raw = raw_recipient(settings, rid)
        override = next((i for i in raw.get("interests", []) if i["name"] == name), {"name": name})
        return jsonify({"interest": {**interest, "paused": is_paused(interest)}, "overrides": override})

    @api.put("/api/recipients/<rid>/interests/<name>")
    def update_interest(rid, name):
        try:
            body = json_body()
            if body is None or body.get("name", name) != name:
                return err("PUT cannot rename an interest", 400)
            inherit = body.get("inherit", [])
            if not isinstance(inherit, list) or any(not isinstance(k, str) for k in inherit):
                return err("inherit must be a list of setting names", 400)
            patch = sparse_interest({k: v for k, v in {**body, "name": name}.items() if k != "inherit"})
            # Validate reset field names and prevent identity changes.
            from newspipeline.recipients import INTEREST_DEFAULTS
            if any(k not in INTEREST_DEFAULTS for k in inherit):
                return err("unknown inherit field", 400)
            with config.mutation_lock:
                settings = config.load_settings()
                if find(settings, rid, name) is None:
                    return err("Interest not found", 404)
                local = save_override(raw_recipient(settings, rid), name, patch)
                for key in inherit:
                    local.pop(key, None)
                check(settings)
                config.save_settings(settings)
            return jsonify({"interest": find(settings, rid, name), "overrides": local})
        except ValueError as exc:
            return err(str(exc), 400)

    @api.delete("/api/recipients/<rid>/interests/<name>")
    def delete_interest(rid, name):
        with config.mutation_lock:
            settings = config.load_settings()
            if find(settings, rid, name) is None:
                return err("Interest not found", 404)
            save_override(raw_recipient(settings, rid), name, {"enabled": False})
            config.save_settings(settings)
        return jsonify({"deleted": name})

    @api.post("/api/recipients/<rid>/interests/<name>/rename")
    def rename_interest(rid, name):
        body = json_body() or {}
        new = body.get("to")
        if not isinstance(new, str) or not new.strip() or "/" in new or new.strip() == name:
            return err("to must be a different non-empty interest name without '/'", 400)
        new = new.strip()
        with config.mutation_lock:
            settings = config.load_settings()
            old = find(settings, rid, name)
            if old is None:
                if find(settings, rid, new) is not None:
                    return jsonify({"renamed": True, "already_renamed": True, "runs_repointed": 0})
                return err("Interest not found", 404)
            if find(settings, rid, new) is not None:
                return err("Interest already exists", 409)
            before = effective_interests(settings)
            raw = raw_recipient(settings, rid)
            save_override(raw, name, {"enabled": False})
            save_override(raw, new, {k: v for k, v in {**old, "name": new}.items() if k not in ("recipient_id", "recipient_email")})
            check(settings)
            after = effective_interests(settings)
            affected = [i["recipient_id"] for i in before if i.get("recipient_id") and i["name"] == name
                        and not any(j.get("recipient_id") == i["recipient_id"] and j["name"] == name for j in after)]
            for child_id in affected:
                if child_id == rid:
                    continue
                if any(i.get("recipient_id") == child_id and i["name"] == new for i in before):
                    return err("Destination interest exists in an inheriting recipient", 409)
                child = raw_recipient(settings, child_id)
                child_override = next((i for i in child.get("interests", []) if i["name"] == name), None)
                if child_override is not None:
                    # Move the descendant's own sparse preferences, rather
                    # than replacing them with the parent's effective values.
                    overrides = {**child_override, "name": new}
                    child["interests"].remove(child_override)
                    save_override(child, new, overrides)
            check(settings)
            try:
                count = app.config["STORE"].rename_interests(name, new, affected)
            except sqlite3.IntegrityError:
                return err("Destination has retained history; choose another name", 409)
            except ValueError as exc:
                return err(str(exc), 409)
            config.save_settings(settings)
        return jsonify({"renamed": True, "from": name, "to": new, "runs_repointed": count})

    @api.post("/api/recipients/<rid>/interests/<name>/trigger")
    def trigger_interest(rid, name):
        if find(config.load_settings(), rid, name) is None:
            return err("Interest not found", 404)
        scheduler = app.config["SCHEDULER"]
        if not scheduler:
            return err("Scheduler is not running", 503)
        error = scheduler.trigger_now(name, rid)
        return err(error, 409) if error else (jsonify({"triggered": True}), 202)

    @api.get("/api/recipients/<rid>/status")
    def recipient_status(rid):
        settings = config.load_settings()
        if raw_recipient(settings, rid) is None:
            return err("Recipient not found", 404)
        return jsonify({"interests": [{"name": i["name"], "recipient_id": rid, "paused": is_paused(i),
                                       "recent_runs": app.config["STORE"].list_runs(i["name"], recipient_id=rid)}
                                      for i in effective_interests(settings, rid)],
                        "deliveries": app.config["STORE"].list_deliveries(rid)})

    @api.post("/api/recipients/<rid>/interests/<name>/feeds")
    def add_feed(rid, name):
        from newspipeline.web.app import _validate_feed
        feed, error = _validate_feed(json_body())
        if error:
            return err(error, 400)
        with config.mutation_lock:
            settings = config.load_settings()
            interest = find(settings, rid, name)
            if interest is None:
                return err("Interest not found", 404)
            feeds = [*interest["feeds"], feed]
            save_override(raw_recipient(settings, rid), name, {"feeds": feeds})
            config.save_settings(settings)
        return jsonify({"feeds": feeds}), 201

    @api.delete("/api/recipients/<rid>/interests/<name>/feeds/<int:index>")
    def remove_feed(rid, name, index):
        with config.mutation_lock:
            settings = config.load_settings()
            interest = find(settings, rid, name)
            if interest is None or not 0 <= index < len(interest["feeds"]):
                return err("Feed not found", 404)
            feeds = interest["feeds"]
            feeds.pop(index)
            save_override(raw_recipient(settings, rid), name, {"feeds": feeds})
            config.save_settings(settings)
        return jsonify({"feeds": feeds})

    @api.get("/api/deliveries")
    def deliveries():
        from flask import request
        return jsonify({"deliveries": app.config["STORE"].list_deliveries(request.args.get("recipient", ""))})

    @api.post("/api/deliveries/<int:run_id>/retry")
    def retry_delivery(run_id):
        from flask import request
        if not app.config["STORE"].retry_delivery(run_id, request.args.get("recipient", "")):
            return err("Failed delivery not found in this recipient scope", 404)
        return jsonify({"queued": True}), 202

    app.register_blueprint(api)
