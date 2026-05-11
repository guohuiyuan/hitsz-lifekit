import argparse
import io
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None:
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            try:
                setattr(sys, _stream_name, io.TextIOWrapper(_stream.buffer, encoding="utf-8", line_buffering=True))
            except Exception:
                pass

DEFAULT_BASE_URL = "http://campusqa.hitsz.edu.cn"
DEFAULT_HEADERS = {
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "User-Agent": "hitsz-lifekit-campusqa/0.1",
}


def make_url(base_url, path, params=None):
    base_url = base_url.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    url = base_url + path
    if params:
        query = urllib.parse.urlencode(params)
        url = url + "?" + query
    return url


def get_json(base_url, path, params=None, timeout=15):
    url = make_url(base_url, path, params)
    request = urllib.request.Request(url, headers=DEFAULT_HEADERS, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read().decode("utf-8")
            return json.loads(data)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"GET {url} failed: HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GET {url} failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GET {url} returned invalid JSON: {exc}") from exc


def fetch_categories(base_url, timeout):
    return get_json(base_url, "/api/categories", timeout=timeout)


def fetch_questions_page(base_url, page, per_page, sort, timeout):
    params = {"page": page, "per_page": per_page, "sort": sort}
    return get_json(base_url, "/api/questions", params=params, timeout=timeout)


def fetch_question_detail(base_url, question_id, timeout):
    return get_json(base_url, f"/api/questions/{question_id}", timeout=timeout)


def fetch_official_search(base_url, keyword, page, per_page, answer_page, timeout):
    params = {"keyword": keyword, "page": page, "per_page": per_page, "answer_page": answer_page}
    return get_json(base_url, "/api/search", params=params, timeout=timeout)


def fetch_ai_chat(base_url, keyword, timeout):
    url = make_url(base_url, "/api/ai-chat", {"keyword": keyword})
    headers = dict(DEFAULT_HEADERS)
    headers["Accept"] = "text/event-stream, */*"
    request = urllib.request.Request(url, headers=headers, method="GET")
    parts = []
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "{}":
                    continue
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                parts.append(str(data.get("text") or ""))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"GET {url} failed: HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GET {url} failed: {exc.reason}") from exc
    return {"query": keyword, "answer": "".join(parts)}


def fetch_question_summaries(base_url, per_page, sort, max_pages, timeout):
    summaries = []
    seen = set()
    total = 0
    page = 1
    while True:
        if max_pages and page > max_pages:
            break
        page_data = fetch_questions_page(base_url, page, per_page, sort, timeout)
        questions = page_data.get("questions") or []
        total = max(total, int_or_zero(page_data.get("total")))
        if not questions:
            break
        for item in questions:
            question_id = int_or_zero(item.get("id"))
            if question_id and question_id not in seen:
                summaries.append(item)
                seen.add(question_id)
        if total and len(seen) >= total:
            break
        page += 1
    return {"total": total, "count": len(summaries), "questions": summaries}


def int_or_zero(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def collect_text(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float, bool)):
        return [str(value)]
    if isinstance(value, list):
        items = []
        for item in value:
            items.extend(collect_text(item))
        return items
    if isinstance(value, dict):
        items = []
        for item in value.values():
            items.extend(collect_text(item))
        return items
    return [str(value)]


def normalize_text(text):
    return re.sub(r"\s+", "", text).lower()


def query_terms(query):
    terms = [term.lower() for term in re.findall(r"[0-9A-Za-z]+|[\u4e00-\u9fff]+", query)]
    compact = normalize_text(query)
    grams = []
    if len(compact) >= 2:
        grams = [compact[index:index + 2] for index in range(len(compact) - 1)]
    return list(dict.fromkeys([compact] + terms + grams))


def score_item(query, item):
    haystack = normalize_text("\n".join(collect_text(item)))
    terms = [term for term in query_terms(query) if term]
    score = 0
    compact = normalize_text(query)
    if compact and compact in haystack:
        score += 100
    for term in terms:
        if term in haystack:
            score += max(2, len(term))
    return score


def search_questions(base_url, query, per_page, sort, max_pages, top, include_details, timeout):
    summaries = fetch_question_summaries(base_url, per_page, sort, max_pages, timeout)
    scored = []
    for summary in summaries["questions"]:
        score = score_item(query, summary)
        if score > 0:
            scored.append((score, int_or_zero(summary.get("id")), summary))
    scored.sort(key=lambda item: (-item[0], item[1] or 10**12))
    results = []
    for score, question_id, summary in scored[:top]:
        result = {"score": score, "id": question_id, "summary": summary}
        if include_details and question_id:
            result["detail"] = fetch_question_detail(base_url, question_id, timeout)
        results.append(result)
    return {
        "query": query,
        "searched_count": summaries["count"],
        "total": summaries["total"],
        "count": len(results),
        "results": results,
    }


def official_search_questions(base_url, query, page, per_page, answer_page, top, include_details, timeout):
    data = fetch_official_search(base_url, query, page, per_page, answer_page, timeout)
    results = []
    seen = set()
    questions_by_id = {int_or_zero(question.get("id")): question for question in data.get("questions") or []}

    for answer in data.get("answers") or []:
        question_id = int_or_zero(answer.get("question_id") or answer.get("id"))
        if not question_id or question_id in seen:
            continue
        seen.add(question_id)
        summary = questions_by_id.get(question_id) or {
            "id": question_id,
            "title": answer.get("question_title"),
            "category_name": answer.get("question_category"),
            "relevance_score": answer.get("relevance_score"),
        }
        score = max(float_or_zero(answer.get("relevance_score")), float_or_zero(summary.get("relevance_score")))
        result = {
            "score": score,
            "id": question_id,
            "summary": summary,
            "source": "official_search_answers",
            "detail": {
                "question": summary,
                "answers": [answer],
            },
        }
        if include_details:
            detail = fetch_question_detail(base_url, question_id, timeout)
            if detail.get("answers"):
                result["detail"] = detail
            else:
                result["detail"] = {"question": detail.get("question") or summary, "answers": [answer]}
        results.append(result)
        if len(results) >= top:
            break

    if len(results) < top:
        for question in data.get("questions") or []:
            question_id = int_or_zero(question.get("id"))
            if not question_id or question_id in seen:
                continue
            seen.add(question_id)
            result = {
                "score": float_or_zero(question.get("relevance_score")),
                "id": question_id,
                "summary": question,
                "source": "official_search_questions",
            }
            if include_details:
                result["detail"] = fetch_question_detail(base_url, question_id, timeout)
            results.append(result)
            if len(results) >= top:
                break

    return {
        "query": query,
        "searched_count": len(data.get("questions") or []),
        "total": int_or_zero(data.get("total_questions")),
        "total_answers": int_or_zero(data.get("total_answers")),
        "page": data.get("page", page),
        "per_page": data.get("per_page", per_page),
        "count": len(results),
        "results": results,
    }


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def float_or_zero(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def answer_question(base_url, query, page, per_page, answer_page, top, min_score, include_details, timeout):
    search_data = official_search_questions(base_url, query, page, per_page, answer_page, top, include_details, timeout)
    usable_results = [result for result in search_data["results"] if result["score"] >= min_score]
    return {
        "query": query,
        "searched_count": search_data["searched_count"],
        "total": search_data["total"],
        "total_answers": search_data.get("total_answers", 0),
        "count": len(usable_results),
        "min_score": min_score,
        "results": usable_results,
    }


def format_markdown_answer(answer_data, base_url):
    lines = [f"根据 CampusQA 检索结果：`{answer_data['query']}`", ""]
    if not answer_data["results"]:
        lines.extend([
            "**结论**",
            "CampusQA 暂未检索到直接对应答案。",
            "",
            "**建议**",
            "- 可以换一个更具体的关键词重试。",
            "- 涉及具体办事政策时，以学校最新通知或负责部门答复为准。",
        ])
        return "\n".join(lines)

    primary = answer_data["results"][0]
    primary_detail = primary.get("detail") or {}
    primary_question = primary_detail.get("question") or primary.get("summary") or {}
    primary_answers = primary_detail.get("answers") or []
    primary_title = clean_text(primary_question.get("title")) or f"问题 {primary.get('id')}"
    lines.extend([
        "**结论**",
        f"最相关的 CampusQA 问题是「{primary_title}」。",
        "",
    ])

    if primary_answers:
        lines.append("**官方回答**")
        for index, answer in enumerate(primary_answers, 1):
            content = clean_text(answer.get("content"))
            if content:
                lines.append(f"{index}. {content}")
        lines.append("")
    else:
        lines.extend([
            "**官方回答**",
            "该问题详情中没有可用回答内容。",
            "",
        ])

    question_content = clean_text(primary_question.get("content"))
    if question_content:
        lines.extend([
            "**问题原文**",
            question_content,
            "",
        ])

    lines.append("**依据**")
    for index, result in enumerate(answer_data["results"], 1):
        detail = result.get("detail") or {}
        question = detail.get("question") or result.get("summary") or {}
        answers = detail.get("answers") or []
        question_id = result.get("id") or question.get("id")
        title = clean_text(question.get("title")) or f"问题 {question_id}"
        category = clean_text(question.get("category_name"))
        endpoint = f"/api/questions/{question_id}" if question_id else "/api/questions/{id}"
        lines.append(f"- **Source {index}**: CampusQA 问题 ID `{question_id}`，标题「{title}」，分类「{category}」，接口 `{endpoint}`，匹配分 `{result['score']}`。")
        for answer in answers[:2]:
            content = clean_text(answer.get("content"))
            if content:
                lines.append(f"  - 回答摘录：{content}")
    lines.extend([
        "",
        "**链接**",
        f"- CampusQA: {base_url.rstrip('/')}/all",
        "",
        "**提醒**",
        "- 涉及政策、时间、费用、地点等信息时，请以学校最新通知或负责部门答复为准。",
    ])
    return "\n".join(lines)


def write_json(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def build_parser():
    parser = argparse.ArgumentParser(prog="campusqa.py")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=int, default=15)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("categories")

    questions_parser = subparsers.add_parser("questions")
    questions_parser.add_argument("--page", type=int, default=1)
    questions_parser.add_argument("--per-page", type=int, default=100)
    questions_parser.add_argument("--sort", default="created_at")

    detail_parser = subparsers.add_parser("detail")
    detail_parser.add_argument("id", type=int)

    search_parser = subparsers.add_parser("search")
    search_parser.add_argument("query")
    search_parser.add_argument("--page", type=int, default=1)
    search_parser.add_argument("--per-page", type=int, default=20)
    search_parser.add_argument("--answer-page", type=int, default=1)
    search_parser.add_argument("--top", type=int, default=5)
    search_parser.add_argument("--no-details", action="store_true")

    local_search_parser = subparsers.add_parser("local-search")
    local_search_parser.add_argument("query")
    local_search_parser.add_argument("--per-page", type=int, default=100)
    local_search_parser.add_argument("--sort", default="created_at")
    local_search_parser.add_argument("--max-pages", type=int, default=10)
    local_search_parser.add_argument("--top", type=int, default=5)
    local_search_parser.add_argument("--no-details", action="store_true")

    ai_chat_parser = subparsers.add_parser("ai-chat")
    ai_chat_parser.add_argument("query")

    answer_parser = subparsers.add_parser("answer")
    answer_parser.add_argument("query")
    answer_parser.add_argument("--page", type=int, default=1)
    answer_parser.add_argument("--per-page", type=int, default=20)
    answer_parser.add_argument("--answer-page", type=int, default=1)
    answer_parser.add_argument("--top", type=int, default=3)
    answer_parser.add_argument("--min-score", type=float, default=1.0)
    answer_parser.add_argument("--with-details", action="store_true")
    answer_parser.add_argument("--format", choices=["markdown", "json"], default="markdown")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "categories":
            write_json(fetch_categories(args.base_url, args.timeout))
        elif args.command == "questions":
            write_json(fetch_questions_page(args.base_url, args.page, args.per_page, args.sort, args.timeout))
        elif args.command == "detail":
            write_json(fetch_question_detail(args.base_url, args.id, args.timeout))
        elif args.command == "search":
            write_json(official_search_questions(args.base_url, args.query, args.page, args.per_page, args.answer_page, args.top, not args.no_details, args.timeout))
        elif args.command == "local-search":
            write_json(search_questions(args.base_url, args.query, args.per_page, args.sort, args.max_pages, args.top, not args.no_details, args.timeout))
        elif args.command == "ai-chat":
            write_json(fetch_ai_chat(args.base_url, args.query, args.timeout))
        elif args.command == "answer":
            answer_data = answer_question(args.base_url, args.query, args.page, args.per_page, args.answer_page, args.top, args.min_score, args.with_details, args.timeout)
            if args.format == "json":
                write_json(answer_data)
            else:
                print(format_markdown_answer(answer_data, args.base_url))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
