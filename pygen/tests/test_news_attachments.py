from types import SimpleNamespace

import pytest

from api import GenerateRequest, NewsArticle, _download_news_attachments
from llm_agent import LLMAgent
from news_attachments import normalize_news_attachments


def test_body_and_pdf_button_are_preserved_as_independent_outputs():
    content = """
    <article>
      <p>The commission imposed civil financial penalties.</p>
      <a class="download-button" href="/files/public-censure.pdf">Download File</a>
    </article>
    """
    item = {
        "sourceUrl": "https://example.com/news/public-censure/",
        "content": content,
    }

    attachments = normalize_news_attachments(item)

    assert item["content"] == content
    assert attachments == [{
        "id": "1",
        "name": "Download File",
        "url": "https://example.com/files/public-censure.pdf",
        "fileType": "pdf",
        "localPath": None,
        "isLocal": False,
    }]


def test_embedded_and_plain_text_pdf_urls_are_detected_and_deduplicated():
    content = """
    <main>
      <object type="application/pdf" data="/files/notice.pdf"></object>
      <p>Backup: https://example.com/files/notice.pdf</p>
      <iframe src="/files/appendix.pdf"></iframe>
    </main>
    """
    attachments = normalize_news_attachments({
        "sourceUrl": "https://example.com/news/1",
        "content": content,
    })

    assert [item["url"] for item in attachments] == [
        "https://example.com/files/notice.pdf",
        "https://example.com/files/appendix.pdf",
    ]


def test_download_button_without_file_extension_is_kept_as_generic_file():
    attachments = normalize_news_attachments({
        "sourceUrl": "https://example.com/news/1",
        "content": '<button data-url="/download?id=42">Download File</button>',
    })
    assert attachments[0]["url"] == "https://example.com/download?id=42"
    assert attachments[0]["fileType"] == "file"


def test_explicit_attachment_aliases_are_normalized_and_deduplicated():
    attachments = normalize_news_attachments({
        "sourceUrl": "https://example.com/news/1",
        "pdfUrl": "/files/a.pdf",
        "attachments": [
            {"downloadUrl": "https://example.com/files/a.pdf", "name": "Notice"},
            {"url": "/files/b.docx", "filename": "Appendix"},
        ],
    })
    assert len(attachments) == 2
    assert attachments[0]["name"] == "Notice"
    assert attachments[0]["fileType"] == "pdf"
    assert attachments[1]["fileType"] == "docx"


def test_navigation_and_footer_download_links_are_ignored():
    attachments = normalize_news_attachments({
        "sourceUrl": "https://example.com/news/1",
        "content": """
          <nav><a href="/site-map.pdf">Download PDF</a></nav>
          <footer><a href="/annual.pdf">Annual report PDF</a></footer>
          <article><p>Body only.</p></article>
        """,
    })
    assert attachments == []


def test_direct_pdf_detail_url_becomes_an_attachment():
    attachments = normalize_news_attachments({
        "title": "Circular",
        "sourceUrl": "https://example.com/files/circular.pdf",
        "content": "",
    })
    assert attachments[0]["name"] == "Circular"
    assert attachments[0]["fileType"] == "pdf"


def test_invalid_declared_file_type_cannot_escape_the_supported_contract():
    attachments = normalize_news_attachments({
        "sourceUrl": "https://example.com/news/1",
        "attachments": [{
            "url": "/files/notice.pdf",
            "name": "Notice",
            "fileType": "application/pdf",
        }],
    })
    assert attachments[0]["fileType"] == "pdf"


def test_news_article_api_contract_exposes_attachments():
    article = NewsArticle(
        id="1",
        title="Notice",
        date="2026-07-16",
        source="Example",
        sourceUrl="https://example.com/news/1",
        content="<p>Body</p>",
        attachments=[{
            "id": "1",
            "name": "Notice PDF",
            "url": "https://example.com/files/notice.pdf",
            "fileType": "pdf",
        }],
    )
    payload = article.model_dump() if hasattr(article, "model_dump") else article.dict()
    assert payload["content"] == "<p>Body</p>"
    assert payload["attachments"][0]["fileType"] == "pdf"


def test_codegen_context_requires_an_independent_attachment_channel():
    agent = object.__new__(LLMAgent)
    issues = agent._check_context_issues(
        "article = {'content': detail_html}",
        {"needs_news_attachments": True},
    )
    assert any(issue.code == "NEWS_ATTACHMENT_001" for issue in issues)

    valid_issues = agent._check_context_issues(
        "article = {'content': body_html, 'attachments': pdf_links}",
        {"needs_news_attachments": True},
    )
    assert all(issue.code != "NEWS_ATTACHMENT_001" for issue in valid_issues)


@pytest.mark.asyncio
async def test_downloader_preserves_body_and_marks_pdf_local(monkeypatch, tmp_path):
    import api
    import httpx

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/pdf"}
        content = b"%PDF-1.7 test"

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _url, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(api, "config", SimpleNamespace(output_dir=tmp_path / "py"))
    monkeypatch.setattr(api, "_add_log", lambda *_args, **_kwargs: None)

    body = "<p>Body remains available.</p>"
    articles = [{
        "sourceUrl": "https://example.com/news/1",
        "content": body,
        "attachments": [{
            "id": "1",
            "name": "notice.pdf",
            "url": "https://example.com/files/notice.pdf",
            "fileType": "pdf",
            "localPath": None,
            "isLocal": False,
        }],
    }]
    request = GenerateRequest(
        url="https://example.com/news/",
        startDate="2026-01-01",
        endDate="2026-12-31",
        outputScriptName="crawler.py",
        runMode="news_sentiment",
        downloadReport="yes",
    )

    downloaded, output_dir = await _download_news_attachments(
        task_id="attachment-test",
        articles=articles,
        request=request,
    )

    assert downloaded == 1
    assert output_dir is not None
    assert articles[0]["content"] == body
    assert articles[0]["attachments"][0]["isLocal"] is True
    assert articles[0]["attachments"][0]["localPath"].endswith("1_notice.pdf")
    assert (output_dir / "1_notice.pdf").read_bytes().startswith(b"%PDF")
