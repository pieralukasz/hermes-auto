from hermes_auto_github import GitHub, Source


class Pages:
    def get(self, path, **params):
        assert path == "/notifications"
        start = (params["page"] - 1) * params["per_page"]
        return [{"id": str(i)} for i in range(start, min(start + params["per_page"], 501))]


def test_notifications_do_not_stop_after_ten_pages():
    client = GitHub(Source.defaults(), http=Pages())
    assert len(client.notifications(0)) == 501
