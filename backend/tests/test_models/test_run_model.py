from __future__ import annotations

from app.models import Target, Tag, TargetTag, Job, Run


def test_run_model(db) -> None:
    target = Target(
        name="Test Database",
        slug="test-db-run",
        plugin_name="pihole",
        plugin_config_json='{"base_url":"http://pihole.local","token":"abc"}',
    )
    db.add(target)
    db.commit()
    db.refresh(target)

    # Tag + job by tag
    tag = Tag(display_name="Daily Backup")
    db.add(tag)
    db.commit()
    db.refresh(tag)
    db.add(TargetTag(target_id=target.id, tag_id=tag.id, origin="DIRECT"))
    db.commit()

    job = Job(
        tag_id=tag.id,
        name="Daily Backup",
        schedule_cron="0 2 * * *",
        enabled=True,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    run = Run(
        job_id=job.id,
        status="success",
        message="Backup completed successfully",
        artifact_path="/backups/test-db-2024-01-01.sql",
        artifact_bytes=1024000,
        sha256="a1b2c3d4e5f6789012345678901234567890abcdef1234567890abcdef123456",
        logs_text="Starting backup...\nBackup completed successfully",
    )

    db.add(run)
    db.commit()
    db.refresh(run)

    assert run.id is not None
    assert run.job_id == job.id
    assert run.status == "success"
    assert run.message == "Backup completed successfully"
    assert run.artifact_path == "/backups/test-db-2024-01-01.sql"
    assert run.artifact_bytes == 1024000
    assert run.sha256 == "a1b2c3d4e5f6789012345678901234567890abcdef1234567890abcdef123456"
    assert run.logs_text == "Starting backup...\nBackup completed successfully"
    assert run.started_at is not None
    assert run.finished_at is None
    assert run.job == job
    assert job.runs == [run]


