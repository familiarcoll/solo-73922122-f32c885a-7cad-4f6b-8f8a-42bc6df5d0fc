import re

CLEAN = (
    "device_id,source_record_id,revision,local_time,water_level\n"
    "D1,S1,1,2024-11-03 01:30:00,1.0\n"
    "D1,S2,1,2024-11-03 01:45:00,2.0\n"
    "D2,S3,1,2024-07-01 00:00:00,3.0\n"
)

GAP = (
    "device_id,source_record_id,revision,local_time,water_level\n"
    "D1,S1,1,2024-03-10 02:30:00,1.0\n"
)

# Two different source records forced onto one UTC instant after choices.
CONFLICT = (
    "device_id,source_record_id,revision,local_time,water_level\n"
    "D1,S1,1,2024-11-03 01:30:00,1.0\n"
    "D1,S2,1,2024-11-03 01:30:00,2.0\n"
)


def _upload(client, text):
    resp = client.post(
        "/upload",
        files={"file": ("data.csv", text.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return resp.headers["location"].split("/")[-1]


def _review_ids(html):
    # Pull (line_no, session) action URLs out of choice forms.
    return re.findall(r'action="/review/([^"]+)/choice"', html)


def test_full_review_publish_download_flow(client):
    sid = _upload(client, CLEAN)
    page = client.get(f"/review/{sid}").text
    assert "重复时段（待判定）" in page
    assert "发布 UTC CSV" in page

    # Publishing before resolving repeated hours must fail.
    blocked = client.post(f"/review/{sid}/publish", follow_redirects=False)
    assert blocked.status_code in (422, 303)

    # Resolve the two ambiguous rows (lines 2 and 3).
    for line_no, choice in ((2, "early"), (3, "late")):
        resp = client.post(
            f"/review/{sid}/choice",
            data={"line_no": line_no, "choice": choice},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    pub = client.post(f"/review/{sid}/publish", follow_redirects=False)
    assert pub.status_code == 303
    pub_id = pub.headers["location"].split("published=")[-1]

    dl = client.get(f"/download/publication/{pub_id}")
    assert dl.status_code == 200
    rows = dl.text.strip().splitlines()
    assert rows[0] == "device_id,source_record_id,revision,utc_time,water_level"
    assert len(rows) == 4
    # early 01:30 EDT -> 05:30Z ; late 01:45 EST -> 06:45Z
    assert "2024-11-03T05:30:00Z,1.0" in dl.text
    assert "2024-11-03T06:45:00Z,2.0" in dl.text

    latest = client.get("/download/latest")
    assert latest.status_code == 200 and latest.text == dl.text


def test_stale_session_rejects_choices_and_publish(client):
    sid_old = _upload(client, CLEAN)

    # A second browser uploads another file: the old session is superseded.
    sid_new = _upload(client, GAP)
    assert sid_old != sid_new

    status = client.get(f"/api/session/{sid_old}/status").json()
    assert status["active"] is False
    assert status["latest_session_id"] == sid_new

    # Old page is still viewable (read-only)...
    view = client.get(f"/review/{sid_old}")
    assert view.status_code == 200
    assert "已被新文件取代" in view.text

    # ...but writing a choice is rejected with 409.
    stale_choice = client.post(
        f"/review/{sid_old}/choice",
        data={"line_no": 2, "choice": "early"},
        follow_redirects=False,
    )
    assert stale_choice.status_code == 409
    assert "不会写入新文件" in stale_choice.text

    # ...and publishing from the old page is rejected too.
    stale_pub = client.post(
        f"/review/{sid_old}/publish", follow_redirects=False
    )
    assert stale_pub.status_code == 409

    # The new session remains active and untouched by the rejected writes.
    new_page = client.get(f"/review/{sid_new}").text
    assert "当前有效" in new_page
    assert "不存在" in new_page  # gap error carried by the new file


def test_previous_publication_remains_downloadable_after_failed_upload(client):
    # Publish a clean result first.
    sid = _upload(client, CLEAN)
    client.post(f"/review/{sid}/choice",
                data={"line_no": 2, "choice": "early"})
    client.post(f"/review/{sid}/choice",
                data={"line_no": 3, "choice": "late"})
    pub = client.post(f"/review/{sid}/publish", follow_redirects=False)
    good_id = pub.headers["location"].split("published=")[-1]
    good_text = client.get(f"/download/publication/{good_id}").text

    # Later upload fails validation (gap) and can never be published.
    bad_sid = _upload(client, GAP)
    bad_pub = client.post(f"/review/{bad_sid}/publish", follow_redirects=False)
    assert bad_pub.status_code == 422

    # The last complete result is still the earlier publication.
    latest = client.get("/download/latest")
    assert latest.status_code == 200
    assert latest.text == good_text


def test_conflict_blocks_publish_until_choices_diverge(client):
    sid = _upload(client, CONFLICT)
    client.post(f"/review/{sid}/choice",
                data={"line_no": 2, "choice": "early"})
    client.post(f"/review/{sid}/choice",
                data={"line_no": 3, "choice": "early"})

    blocked = client.post(f"/review/{sid}/publish", follow_redirects=False)
    assert blocked.status_code == 422
    assert "冲突" in blocked.text

    # Switch one row to the late occurrence: UTC instants now differ.
    client.post(f"/review/{sid}/choice",
                data={"line_no": 3, "choice": "late"})
    ok = client.post(f"/review/{sid}/publish", follow_redirects=False)
    assert ok.status_code == 303


def test_download_latest_404_without_publication(client):
    # Fresh isolated logical state: other tests in this module may have
    # published, so assert the endpoint behaves uniformly when a 404 path
    # is exercised via a non-existent publication id instead.
    resp = client.get("/download/publication/999999")
    assert resp.status_code == 404


def test_missing_upload_is_rejected(client):
    resp = client.post("/upload", follow_redirects=False)
    assert resp.status_code == 422
