from __future__ import annotations

import pytest

from app.ocr_client import (
    GLM_DEFAULT_PROMPT,
    GLM_MAAS_DEFAULT_MODEL,
    GLM_OPENAI_DEFAULT_MODEL,
    build_glm_maas_payload,
    build_glm_openai_chat_payload,
    extract_glm_maas_markdown,
    extract_glm_openai_markdown,
    normalize_glm_file_reference,
)


def test_normalize_glm_file_reference_accepts_urls_data_uri_and_raw_b64() -> None:
    assert normalize_glm_file_reference("https://example.com/file.png") == "https://example.com/file.png"
    assert (
        normalize_glm_file_reference("data:image/png;base64,AAAA")
        == "data:image/png;base64,AAAA"
    )
    converted = normalize_glm_file_reference("QUJDRA==")
    assert converted == "data:image/png;base64,QUJDRA=="


def test_build_glm_maas_payload_contract_shape() -> None:
    payload = build_glm_maas_payload(
        file_ref="https://example.com/a.png",
        model=GLM_MAAS_DEFAULT_MODEL,
    )
    assert payload["model"] == GLM_MAAS_DEFAULT_MODEL
    assert payload["file"] == "https://example.com/a.png"


def test_build_glm_openai_payload_contract_shape() -> None:
    payload = build_glm_openai_chat_payload(
        image_b64="AAAA",
        prompt=GLM_DEFAULT_PROMPT,
        model=GLM_OPENAI_DEFAULT_MODEL,
    )
    assert payload["model"] == GLM_OPENAI_DEFAULT_MODEL
    messages = payload["messages"]
    assert isinstance(messages, list) and len(messages) == 1
    content = messages[0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_extract_glm_maas_markdown_from_nested_shapes() -> None:
    assert extract_glm_maas_markdown({"markdown": "# title"}) == "# title"
    assert (
        extract_glm_maas_markdown({"result": {"data": {"content": "nested markdown"}}})
        == "nested markdown"
    )


def test_extract_glm_openai_markdown_from_string_and_content_parts() -> None:
    assert (
        extract_glm_openai_markdown({"choices": [{"message": {"content": "plain markdown"}}]})
        == "plain markdown"
    )
    assert (
        extract_glm_openai_markdown(
            {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "line one"},
                                {"type": "text", "text": "line two"},
                            ]
                        }
                    }
                ]
            }
        )
        == "line one\nline two"
    )


def test_extract_glm_helpers_raise_for_missing_content() -> None:
    with pytest.raises(ValueError):
        extract_glm_maas_markdown({"result": {}})
    with pytest.raises(ValueError):
        extract_glm_openai_markdown({"choices": [{"message": {"content": []}}]})
