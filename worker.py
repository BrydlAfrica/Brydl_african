"""Standalone background-worker entrypoint (Phase 10).

Optional scale lever: Render runs the web service with the in-process
runner thread by default ($0, single-service deployment). When volume
demands a dedicated worker, provision a second service from this file
(same repo, same database URL, JOBS_ENABLED=1) and set
JOBS_ENABLED=0 on the web service — claiming is a conditional UPDATE,
so web-embedded and dedicated runners can share the queue safely.
"""
from app import create_app
import jobs


def main() -> None:
    """Poll the job queue forever (one pass per poll interval)."""
    app = create_app()
    poll = int(app.config.get("JOBS_POLL_SECONDS", 30))
    app.logger.info("Dedicated worker polling every %ss.", poll)
    while True:
        import time

        jobs.run_due_once(app, limit=25)
        time.sleep(poll)


if __name__ == "__main__":
    main()
