"""Pagination: LIMIT + OFFSET over a deterministic ORDER BY."""


def _ids(resp):
    return [row[0] for row in resp.json()["rows"]]


def test_limit_offset_slices_correctly(client, admin_headers):
    # 10 PRs with ids 101..110; ordered asc, page size 3.
    base = "SELECT pr.id FROM github.pull_requests pr ORDER BY pr.id"
    page1 = client.post("/v1/query", headers=admin_headers,
                        json={"sql": f"{base} LIMIT 3 OFFSET 0"})
    page2 = client.post("/v1/query", headers=admin_headers,
                        json={"sql": f"{base} LIMIT 3 OFFSET 3"})
    page3 = client.post("/v1/query", headers=admin_headers,
                        json={"sql": f"{base} LIMIT 3 OFFSET 6"})

    assert _ids(page1) == [101, 102, 103]
    assert _ids(page2) == [104, 105, 106]      # OFFSET is honoured, not ignored
    assert _ids(page3) == [107, 108, 109]
    # pages do not overlap
    assert set(_ids(page1)) & set(_ids(page2)) == set()
