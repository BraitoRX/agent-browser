import json
from typing import Any, Dict


MAX_VIEW_BYTES = 1024 * 1024


def _bound_records(records, remaining):
    retained = []
    omitted = 0
    for record in records:
        size = len(json.dumps(record, ensure_ascii=True).encode("utf-8")) + 2
        if size > remaining:
            omitted += 1
        else:
            retained.append(record)
            remaining -= size
    return retained, omitted, remaining


async def inspect_workers(runtime: Any) -> Dict[str, Any]:
    page, tab = runtime.require_active()
    workers = page.workers
    result = {
        "tabId": tab.tab_id,
        "workers": [{"url": worker.url[:8192], "type": "dedicated", "urlTruncated": len(worker.url) > 8192}
                    for worker in workers[:500]],
        "omitted": max(0, len(workers) - 500),
        "serviceWorkerNetworkSupported": False,
        "workerDebuggingSupported": False,
        "scope": "active page and current-origin registrations, not all browser workers",
    }
    registrations = await page.evaluate("""async () => {
        if (!("serviceWorker" in navigator)) {
            return {serviceWorkerRegistrations: [], serviceWorkersUnavailable: "API unavailable"};
        }
        try {
            const registrations = await navigator.serviceWorker.getRegistrations();
            const worker = value => value ? {
                scriptURL: value.scriptURL.slice(0, 8192),
                urlTruncated: value.scriptURL.length > 8192,
                state: value.state
            } : null;
            return {
                serviceWorkerRegistrations: registrations.slice(0, 100).map(registration => ({
                    scope: registration.scope.slice(0, 8192),
                    scopeTruncated: registration.scope.length > 8192,
                    active: worker(registration.active),
                    waiting: worker(registration.waiting),
                    installing: worker(registration.installing)
                })),
                registrationsOmitted: Math.max(0, registrations.length - 100)
            };
        } catch (error) {
            return {serviceWorkerRegistrations: [], serviceWorkersUnavailable: String(error.name).slice(0, 256)};
        }
    }""")
    result.update(registrations)
    retained, omitted, remaining = _bound_records(result["workers"], MAX_VIEW_BYTES - 4096)
    result["workers"] = retained
    result["omitted"] += omitted
    retained, omitted, _ = _bound_records(result["serviceWorkerRegistrations"], remaining)
    result["serviceWorkerRegistrations"] = retained
    result["registrationsOmitted"] = result.get("registrationsOmitted", 0) + omitted
    result["outputLimitBytes"] = MAX_VIEW_BYTES
    return result
