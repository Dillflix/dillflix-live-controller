"""Scripted semantic answers for workflow fixtures, not a substitute language parser."""

import json


def answer(item=None, language="unlabeled", status="no_match"):
    return dict(
        match_status="matched" if item else status,
        content_id=item["content_id"] if item else None,
        language=language if item else None,
        reason="Scripted fixture decision",
        evidence=[{"field": "title", "quote": item["title"]}] if item else [],
    )


class FixtureBroadcastModel:
    async def completion(self, model, messages, schema, **kwargs):
        context = json.loads(messages[1]["content"])
        if kwargs.get("name") != "broadcast_selection":
            # Preserve abstention for ambiguous event identity in workflow tests.
            return json.dumps(
                dict(
                    match_status="uncertain",
                    content_id=None,
                    viewing_option_id=None,
                    reason="Unscripted event",
                    evidence=[],
                )
            )
        choices = sorted(
            (i for i in context["candidates"] if i["content_id"] in context["selectable_content_ids"]),
            key=lambda i: i["content_id"],
        )
        # Explicit meanings of labels in the checked-in NFL/NHL fixtures.
        excluded = {"Lions @ Panthers (In French)", "French Broadcast"}
        item = next((i for i in choices if i["title"] not in excluded), None)
        return json.dumps(
            answer(item, "en" if item and item["title"] == "English Broadcast" else "unlabeled")
        )

    async def close(self):
        pass
