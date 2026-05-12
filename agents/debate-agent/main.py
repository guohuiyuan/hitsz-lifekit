import argparse
import asyncio
import httpx
import io
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from typing import Literal

from pydantic import BaseModel, Field

try:
    import agentscope
    from agentscope.agent import ReActAgent
    from agentscope.formatter import OpenAIChatFormatter
    from agentscope.message import Msg
    from agentscope.model import OpenAIChatModel
    from agentscope.pipeline import MsgHub
except ImportError as exc:
    raise SystemExit(
        "缺少 AgentScope 依赖。请先运行：pip install -r agents/debate-agent/requirements.txt"
    ) from exc

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

PROJECT_DIR = Path(__file__).resolve().parent
ENV_FILE = PROJECT_DIR / ".env"
DEFAULT_TOPIC = "大学生是否应该广泛使用生成式 AI 辅助学习？"


@dataclass
class DebateTurn:
    stage: str
    speaker: str
    content: str


@dataclass
class DebateContext:
    topic: str
    affirmative_position: str
    negative_position: str


class CriterionScore(BaseModel):
    criterion: str = Field(description="评分项目")
    affirmative_score: int = Field(description="正方该项得分，0 到 100", ge=0, le=100)
    negative_score: int = Field(description="反方该项得分，0 到 100", ge=0, le=100)
    comment: str = Field(description="该项评分理由")


class StageEvaluation(BaseModel):
    stage: str = Field(description="被评价的赛段")
    affirmative_performance: str = Field(description="正方在该赛段的表现")
    negative_performance: str = Field(description="反方在该赛段的表现")
    judge_comment: str = Field(description="本赛段评委点评")


class JudgeDecision(BaseModel):
    judge_name: str = Field(description="评委名称")
    focus: str = Field(description="本评委的评价侧重点")
    judging_standard: str = Field(description="本评委采用的核心胜负标准")
    affirmative_score: int = Field(description="正方得分，0 到 100", ge=0, le=100)
    negative_score: int = Field(description="反方得分，0 到 100", ge=0, le=100)
    winner: Literal["正方", "反方", "平局"] = Field(description="本评委判定的胜方")
    ballot_reason: str = Field(description="完整投票理由")
    scoring_breakdown: list[CriterionScore] = Field(description="逐项评分")
    stage_evaluations: list[StageEvaluation] = Field(description="逐阶段点评")
    key_clashes: list[str] = Field(description="本场关键交锋")
    affirmative_best_point: str = Field(description="正方最强论点或表现")
    negative_best_point: str = Field(description="反方最强论点或表现")
    affirmative_weakness: str = Field(description="正方主要问题")
    negative_weakness: str = Field(description="反方主要问题")
    decisive_clash: str = Field(description="最关键的攻防交锋")
    advice_affirmative: str = Field(description="给正方的改进建议")
    advice_negative: str = Field(description="给反方的改进建议")
    natural_commentary: str = Field(default="", description="自然语言评委长评")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def resolve_topic(topic: str) -> str:
    topic = topic.strip()
    if topic:
        return topic
    if sys.stdin.isatty():
        user_topic = input("请输入本场辩题（直接回车使用默认辩题）：").strip()
        if user_topic:
            return user_topic
    return DEFAULT_TOPIC


def create_model(model_name: str, api_key: str, base_url: str, stream: bool) -> OpenAIChatModel:
    verify_ssl = env_bool("OPENAI_VERIFY_SSL", True)
    trust_env = env_bool("OPENAI_TRUST_ENV", True)
    client_kwargs = {"base_url": base_url}
    if not verify_ssl or not trust_env:
        client_kwargs["http_client"] = httpx.AsyncClient(verify=verify_ssl, trust_env=trust_env)
    return OpenAIChatModel(
        model_name=model_name,
        api_key=api_key,
        stream=stream,
        client_kwargs=client_kwargs,
        generate_kwargs={"temperature": 0.7},
    )


def create_debater(name: str, side: str, position: str, topic: str, model_name: str, api_key: str, base_url: str, stream: bool) -> ReActAgent:
    sys_prompt = dedent(
        f"""
        你是{name}，在一场中文辩论赛中代表{side}。

        辩题：{topic}
        你的立场：{position}

        你的目标不是堆砌口号，而是用清晰的定义、稳定的判断标准、可解释的因果链、现实场景和有效攻防说服评委。你需要同时做到：
        1. 先明确结论，再给理由。
        2. 每次发言都回应当前阶段任务，不跳阶段。
        3. 主动抓住对方立论、质询回答和质询小结里的定义偷换、因果断裂、证据不足、价值排序混乱或现实可行性问题。
        4. 多用现实场景支撑观点，例如课堂、作业、实习、家庭、平台治理、公共政策、普通人的日常选择；场景必须扣回己方判断标准。
        5. 不只反问一句就结束，要让对方立论在多个现实场景里承受压力，证明你的立场更能解释现实。
        6. 承认必要限制，但把限制转化为己方判断标准的一部分。
        7. 保持辩论锋芒，但不进行人身攻击。
        8. 默认使用中文，语言紧凑、有层次、有现场感。
        9. 永远记住你代表{side}，你的最终任务是证明“{position}”更成立；不得替对方完成论证，不得把对方核心命题当成己方结论。
        10. 可以承认局部风险或例外，但承认后必须立刻转化为己方标准下可处理、可比较、可反打的理由。
        11. 如果你发现上一段上下文里有对己方不利的表述，只能把它解释为对方观点或需要修正的风险，不能继续扩大成己方结论。
        12. 只输出正式发言，不输出思考过程、草稿、字数检查或自我解释。
        """
    ).strip()
    return ReActAgent(
        name=name,
        sys_prompt=sys_prompt,
        model=create_model(model_name, api_key, base_url, stream),
        formatter=OpenAIChatFormatter(),
    )


def create_judge(name: str, focus_prompt: str, model_name: str, api_key: str, base_url: str, stream: bool) -> ReActAgent:
    sys_prompt = dedent(
        f"""
        你是{name}，负责对一场中文辩论赛做独立裁决。

        你的评价侧重点：
        {focus_prompt}

        评分要求：
        1. 分别给正方和反方 0 到 100 分。
        2. 像真实赛后评委一样写自然语言长评，不要输出 JSON，不要输出代码块，不要像机器表格。
        3. 评价要有现场感和可读性：先讲本场胜负手，再讲双方最漂亮和最可惜的地方，再点关键交锋，最后给建议。
        4. 必须点评自由辩论是否做到“一问一答”：有没有正面回答、有没有接住对方问题、回抛的问题是否压迫。
        5. 可以肯定表达效果，但不能被漂亮话替代论证质量。
        6. 如果双方在你的评价维度上难分胜负，可以判为平局。
        7. 输出必须忠于辩论记录，不补充双方没有提出的新论据。
        8. 默认使用中文，可以写得更饱满，让观众看得过瘾。
        9. 末尾必须单独写一行：裁决：正方/反方/平局；比分：正方xx，反方xx。
        """
    ).strip()
    return ReActAgent(
        name=name,
        sys_prompt=sys_prompt,
        model=create_model(model_name, api_key, base_url, stream),
        formatter=OpenAIChatFormatter(),
    )


def create_reporter(model_name: str, api_key: str, base_url: str, stream: bool) -> ReActAgent:
    sys_prompt = dedent(
        """
        你是观点型内容作者，擅长把一场辩论里的精华改写成适合知乎和小红书发布的高传播文案。

        写作要求：
        1. 不要写比赛复盘，不要按赛程交代“谁先说了什么”；读者不需要知道完整比赛过程。
        2. 可以偏颇，可以有鲜明立场，优先站在最终胜方或更有现实解释力的一方。
        3. 把比赛数据当作文章背书：例如三评委投票、平均分、综合结果，但不要堆表格。
        4. 把辩论里的精华攻防、关键比喻、现实场景和胜负手融进文章观点里，写得比普通总结更有冲击力。
        5. 适合知乎和小红书：标题抓人，开头有钩子，中段有现实场景和洞察，结尾有金句和互动引导。
        6. 可以使用少量 emoji 和分段小标题，但不要营销腔过重，不要编造比赛外的数据、政策、论文或新闻。
        7. 忠于辩论记录和评委裁决，但表达可以更锋利、更偏向、更像人写的爆款观点文。
        """
    ).strip()
    return ReActAgent(
        name="平台观点文案官",
        sys_prompt=sys_prompt,
        model=create_model(model_name, api_key, base_url, stream),
        formatter=OpenAIChatFormatter(),
    )


def msg_text(msg: Msg) -> str:
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
            else:
                parts.append(str(item))
        return "\n".join(parts).strip()
    return str(content).strip()


def print_block(title: str, body: str) -> None:
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)
    print(body)


async def speak(agent: ReActAgent, stage: str, instruction: str, turns: list[DebateTurn], context: DebateContext) -> None:
    msg = await agent(Msg("主持人", build_turn_instruction(agent.name, instruction, turns, context), "user"))
    content = clean_stage_content(stage, msg_text(msg))
    turns.append(DebateTurn(stage=stage, speaker=agent.name, content=content))
    print_block(f"{stage}｜{agent.name}", content)


def side_of_speaker(speaker_name: str) -> str:
    if speaker_name.startswith("正方"):
        return "正方"
    if speaker_name.startswith("反方"):
        return "反方"
    return ""


def build_turn_instruction(agent_name: str, stage_instruction: str, turns: list[DebateTurn], context: DebateContext) -> str:
    side = side_of_speaker(agent_name)
    own_position = context.affirmative_position if side == "正方" else context.negative_position
    opponent_side = "反方" if side == "正方" else "正方"
    opponent_position = context.negative_position if side == "正方" else context.affirmative_position
    transcript = build_transcript(context.topic, context.affirmative_position, context.negative_position, turns) if turns else "目前还没有正式发言。"
    return dedent(
        f"""
        重要立场锚点：
        - 辩题：{context.topic}
        - 你是：{agent_name}，代表{side}
        - 你的核心立场：{own_position}
        - 对方核心立场：{opponent_position}
        - 你的不可越界结论：本轮无论如何都要让评委更相信“{own_position}”，不能让评委更相信“{opponent_position}”。
        - 本轮任务：{stage_instruction}

        绝对禁止：
        1. 不得把{opponent_side}的核心论点当成{side}的结论。
        2. 不得替{opponent_side}补强论证；引用对方观点时必须明确是在攻击、限制或转化它。
        3. 不得因为上一段上下文里出现了对{side}不利的话，就继续顺着它论证；必须把它当成对方观点或己方需要化解的风险。
        4. 每次承认风险后，必须立刻说明为什么该风险在{side}标准下可处理、可比较，或为什么仍然不推翻“{own_position}”。
        5. 如果你的草稿中出现了“因此对方更成立”“我方核心论点不成立”这类意思，必须立刻改写为对对方论点的反击或己方的补救。

        已有完整辩论记录：
        {transcript}

        现在请完成本轮正式发言。只输出正式发言，不输出思考过程、草稿、复述任务或自我检查。
        """
    ).strip()


def clean_stage_content(stage: str, content: str) -> str:
    if stage != "自由辩论":
        return content
    content = re.sub(r"^\s*(答|攻|问)\s*[:：]\s*", "", content, flags=re.MULTILINE)
    content = re.sub(r"\s*\n+\s*", " ", content).strip()
    return content


def build_transcript(topic: str, affirmative_position: str, negative_position: str, turns: list[DebateTurn]) -> str:
    header = dedent(
        f"""
        辩题：{topic}
        正方立场：{affirmative_position}
        反方立场：{negative_position}
        """
    ).strip()
    body = "\n\n".join(f"【{turn.stage}｜{turn.speaker}】\n{turn.content}" for turn in turns)
    return f"{header}\n\n{body}"


def statement_instruction(side: str, role: str, statement_no: int, max_words: int) -> str:
    return dedent(
        f"""
        现在进入【陈词{statement_no}】阶段。请以{side}{role}身份完成申论，控制在 {max_words} 字以内。

        必须包含：
        1. 明确己方在本阶段推进的核心命题。
        2. 如果是陈词一，优先完成定义、标准和主论证架构；如果是陈词二，优先补强论证并处理对方已有攻击。
        3. 至少处理一个关键胜负标准或举证责任问题。
        4. 至少使用一个具体的现实场景或生活例子，并说明这个场景为什么支持己方判断标准。
        5. 明确指出后续最值得质询对方的一处立论漏洞。
        6. 只输出正式发言，不输出提纲、草稿或字数检查。
        """
    ).strip()


def cross_exam_question_instruction(side: str, role: str, target: str, max_words: int) -> str:
    return dedent(
        f"""
        现在进入【质询】阶段。请以{side}{role}身份质询{target}，控制在 {max_words} 字以内。

        新国辩式质询要求：
        1. 连续追问，不做长篇陈词。
        2. 问题要短、准、可回答，优先攻击定义、标准、因果链、例证适用性或价值排序。
        3. 每组问题要形成推进：确认前提、逼出承认、把对方带入一个现实场景、指出矛盾或代价。
        4. 必须针对对方刚才的立论、答询或小结中的具体一句话追问，不泛泛发难。
        5. 多质询一点，至少形成 3 个连贯短问，但不要把问题写成大段演讲。
        6. 不替对方回答，不输出主持说明。
        7. 只输出质询方正式发言。
        """
    ).strip()


def cross_exam_answer_instruction(side: str, role: str, questioner: str, max_words: int) -> str:
    return dedent(
        f"""
        现在进入【答询】阶段。请以{side}{role}身份回应{questioner}刚才的质询，控制在 {max_words} 字以内。

        要求：
        1. 正面回答对方最关键的问题，不回避核心前提。
        2. 对不合理预设要即时拆解，并把回答拉回己方标准。
        3. 用一个现实场景反证对方预设，说明己方立论在该场景中更成立。
        4. 回答要短促清楚，体现现场攻防，不重新长篇立论。
        5. 只输出答询方正式发言。
        """
    ).strip()


def inquiry_summary_instruction(side: str, role: str, max_words: int) -> str:
    return dedent(
        f"""
        现在进入【质询小结】阶段。请以{side}{role}身份总结己方在两轮质询中的攻防收益，控制在 {max_words} 字以内。

        必须包含：
        1. 己方通过质询逼出的关键承认或暴露的关键漏洞。
        2. 对方答询中最影响胜负的回避、矛盾或代价。
        3. 点名对方立论在至少一个现实场景里为什么撑不住。
        4. 这些质询收益如何服务于己方判断标准，并为自由辩论留下下一步追问方向。
        5. 不引入全新论点，不做单纯复述。
        6. 只输出正式发言。
        """
    ).strip()


def free_debate_instruction(side: str, round_index: int, max_words: int, is_first: bool = False) -> str:
    relay_rule = (
        "这是自由辩论开问。请抓住前面质询小结或对方立论中的一个关键漏洞，用一个现实场景发起进攻，最后自然抛出一个问题。"
        if is_first
        else "这是自由辩论接力。第一句话必须接住并正面回答对方上一段最后抛出的问题，然后顺着同一个交锋点反打，最后自然抛出一个新问题。"
    )
    return dedent(
        f"""
        现在进入【自由辩论】第 {round_index} 轮。请代表{side}发言，控制在 {max_words} 字以内。

        接力规则：
        {relay_rule}

        要求：
        1. 不要写“答、攻、问”标签，不要分点，不要像模板；要像赛场上一口气说出的一段完整发言。
        2. 整段最多只出现一个问号，且这个问题必须放在最后，给对方下一棒回答。
        3. 不能无视上一问另起炉灶，不能只喊价值口号。
        4. 每次只推进一个关键交锋，但要用现实案例让对方立论在具体场景里经受压力。
        5. 语言要有对抗性和观赏性，让观众看得出你接住了问题、反打了漏洞、把压力抛回给对方。
        6. 只输出正式发言。
        """
    ).strip()


def closing_instruction(side: str, max_words: int) -> str:
    return dedent(
        f"""
        现在进入【总结陈词】阶段。请代表{side}完成四辩总结，控制在 {max_words} 字以内。

        必须包含：
        1. 本场最关键的胜负标准。
        2. 己方在该标准下为什么更成立。
        3. 质询、自由辩中形成的关键战果。
        4. 对方最强反驳为什么不足以翻盘。
        5. 面向评委的最终投票理由。
        6. 不引入全新主论点，只做比赛归纳和裁判说服。
        7. 只输出正式发言。
        """
    ).strip()


async def judge_debate(judge: ReActAgent, transcript: str) -> dict:
    msg = await judge(
        Msg(
            "主持人",
            dedent(
                """
                请阅读以下完整辩论记录，并按你的评审侧重点做出裁决。

                输出要求：
                1. 不要输出 JSON，不要输出代码块，不要写机器式字段清单。
                2. 请像真实评委赛后点评一样写一段完整自然语言长评，可以分成几段，但要连贯、有现场感。
                3. 必须讲清楚：你怎么判胜负、本场最关键的胜负手、双方最漂亮的一段、双方最可惜的问题、自由辩论一问一答接力是否成立、给双方的改进建议。
                4. 评价可以写得更饱满，更像给观众复盘，不要只给结论。
                5. 末尾必须单独写一行：裁决：正方/反方/平局；比分：正方xx，反方xx。
                """
            ).strip()
            + "\n\n"
            + transcript,
            "user",
        )
    )
    raw_commentary = msg_text(msg)
    result = parse_judge_result(raw_commentary, judge.name)
    result["natural_commentary"] = raw_commentary
    print_block(f"评委裁决｜{judge.name}", format_judge_result(result))
    return result


def parse_judge_result(text: str, judge_name: str) -> dict:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return build_fallback_judge_result(judge_name, text)
    try:
        return JudgeDecision.model_validate(normalize_judge_data(data, judge_name, text)).model_dump()
    except Exception:
        return build_fallback_judge_result(judge_name, text)


def normalize_winner(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"正方", "affirmative", "aff", "pro", "for"} or "正" in text:
        return "正方"
    if text in {"反方", "negative", "neg", "con", "against"} or "反" in text:
        return "反方"
    return "平局"


def as_score(value: object, default: int = 70) -> int:
    try:
        return max(0, min(100, int(float(str(value).strip()))))
    except (TypeError, ValueError):
        return default


def pick_any(mapping: dict, keys: tuple[str, ...], default: object = "") -> object:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def normalize_scoring_breakdown(value: object, affirmative_score: int, negative_score: int) -> list[dict]:
    items = value.items() if isinstance(value, dict) else enumerate(value or [])
    normalized = []
    for key, raw in items:
        if isinstance(raw, dict):
            criterion = str(pick_any(raw, ("criterion", "项目", "评分项目"), key))
            aff = as_score(pick_any(raw, ("affirmative_score", "affirmative", "正方", "正方得分"), affirmative_score), affirmative_score)
            neg = as_score(pick_any(raw, ("negative_score", "negative", "反方", "反方得分"), negative_score), negative_score)
            comment = str(pick_any(raw, ("comment", "reason", "reasoning", "理由", "评价", "点评"), ""))
        else:
            criterion = str(key)
            aff = affirmative_score
            neg = negative_score
            comment = str(raw)
        normalized.append(
            {
                "criterion": criterion,
                "affirmative_score": aff,
                "negative_score": neg,
                "comment": comment,
            },
        )
    return normalized


def normalize_stage_evaluations(value: object) -> list[dict]:
    items = value.items() if isinstance(value, dict) else enumerate(value or [])
    normalized = []
    for key, raw in items:
        if isinstance(raw, dict):
            stage = str(pick_any(raw, ("stage", "赛段", "阶段"), key))
            aff = str(pick_any(raw, ("affirmative_performance", "affirmative", "正方", "正方表现"), ""))
            neg = str(pick_any(raw, ("negative_performance", "negative", "反方", "反方表现"), ""))
            comment = str(pick_any(raw, ("judge_comment", "comment", "点评", "评价"), ""))
        else:
            stage = str(key)
            aff = "详见评委点评。"
            neg = "详见评委点评。"
            comment = str(raw)
        normalized.append(
            {
                "stage": stage,
                "affirmative_performance": aff,
                "negative_performance": neg,
                "judge_comment": comment,
            },
        )
    return normalized


def normalize_key_clashes(value: object) -> list[str]:
    if not isinstance(value, list):
        return [str(value)] if value else []
    clashes = []
    for item in value:
        if isinstance(item, dict):
            title = str(pick_any(item, ("clash", "title", "交锋", "name"), "关键交锋"))
            comment = str(pick_any(item, ("comment", "reason", "点评", "评价"), ""))
            clashes.append(f"{title}：{comment}" if comment else title)
        else:
            clashes.append(str(item))
    return clashes


def normalize_judge_data(data: dict, judge_name: str, text: str) -> dict:
    affirmative_score = as_score(data.get("affirmative_score", data.get("正方得分", 70)))
    negative_score = as_score(data.get("negative_score", data.get("反方得分", 70)))
    normalized = {
        "judge_name": str(data.get("judge_name") or data.get("评委") or judge_name),
        "focus": str(data.get("focus") or data.get("侧重点") or "按本评委设定维度进行综合评价"),
        "judging_standard": str(data.get("judging_standard") or data.get("裁判标准") or data.get("胜负标准") or "综合比较双方论证、攻防和说服力"),
        "affirmative_score": affirmative_score,
        "negative_score": negative_score,
        "winner": normalize_winner(data.get("winner") or data.get("胜方")),
        "ballot_reason": str(data.get("ballot_reason") or data.get("投票理由") or data.get("core_reason") or truncate_text(text, 1200)),
        "scoring_breakdown": normalize_scoring_breakdown(data.get("scoring_breakdown") or data.get("逐项评分"), affirmative_score, negative_score),
        "stage_evaluations": normalize_stage_evaluations(data.get("stage_evaluations") or data.get("逐阶段点评")),
        "key_clashes": normalize_key_clashes(data.get("key_clashes") or data.get("关键交锋")),
        "affirmative_best_point": str(data.get("affirmative_best_point") or data.get("正方亮点") or "正方强调工具中性与学习效率提升"),
        "negative_best_point": str(data.get("negative_best_point") or data.get("反方亮点") or "反方强调认知依赖、验证成本与学习真实性"),
        "affirmative_weakness": str(data.get("affirmative_weakness") or data.get("正方问题") or "正方对验证成本与滥用风险回应仍需细化"),
        "negative_weakness": str(data.get("negative_weakness") or data.get("反方问题") or "反方需避免把合理使用直接推成必然依赖"),
        "decisive_clash": str(data.get("decisive_clash") or data.get("最关键交锋") or "AI 使用是否必然替代独立思考"),
        "advice_affirmative": str(data.get("advice_affirmative") or data.get("给正方建议") or "补充可执行的边界、验证机制和防滥用方案。"),
        "advice_negative": str(data.get("advice_negative") or data.get("给反方建议") or "进一步证明广泛使用与能力退化之间的机制链条。"),
    }
    if not normalized["scoring_breakdown"]:
        normalized["scoring_breakdown"] = build_fallback_judge_result(judge_name, text)["scoring_breakdown"]
    if not normalized["stage_evaluations"]:
        normalized["stage_evaluations"] = build_fallback_judge_result(judge_name, text)["stage_evaluations"]
    if not normalized["key_clashes"]:
        normalized["key_clashes"] = build_fallback_judge_result(judge_name, text)["key_clashes"]
    return normalized


def first_match(patterns: list[str], text: str, default: str = "") -> str:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1).strip()
    return default


def extract_score_pair(text: str) -> tuple[int, int]:
    patterns = [
        r"比分[:：]\s*正方\s*(\d{1,3})\s*[，,、 ]+\s*反方\s*(\d{1,3})",
        r"总分[:：]?\s*正方\s*(\d{1,3})\s*(?:vs|VS|比|，|,|\s)\s*反方\s*(\d{1,3})",
        r"正方\s*(\d{1,3})\s*(?:vs|VS|比|，|,|\s)\s*反方\s*(\d{1,3})",
        r"正方得分[:：]?\s*(\d{1,3}).{0,40}?反方得分[:：]?\s*(\d{1,3})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if match:
            return max(0, min(100, int(match.group(1)))), max(0, min(100, int(match.group(2))))
    return 70, 70


def infer_winner(text: str, affirmative_score: int, negative_score: int) -> str:
    if re.search(r"裁决[:：]\s*正方|胜方[:：]?\s*正方|正方\s*胜", text):
        return "正方"
    if re.search(r"裁决[:：]\s*反方|胜方[:：]?\s*反方|反方\s*胜", text):
        return "反方"
    if re.search(r"裁决[:：]\s*平局|平局", text):
        return "平局"
    if affirmative_score > negative_score:
        return "正方"
    if negative_score > affirmative_score:
        return "反方"
    return "平局"


def truncate_text(text: str, limit: int = 240) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit].rstrip() + "……"


def build_fallback_judge_result(judge_name: str, text: str) -> dict:
    affirmative_score, negative_score = extract_score_pair(text)
    winner = infer_winner(text, affirmative_score, negative_score)
    criteria = ["立论建构", "质询攻防", "自由辩推进", "总结收束", "表达与策略"]
    stage_names = ["陈词一", "质询一", "陈词二", "质询二", "质询小结", "自由辩论", "总结陈词"]
    scoring_breakdown = []
    for criterion in criteria:
        match = re.search(
            rf"{criterion}[:：]\s*正方\s*(\d{{1,3}}).*?反方\s*(\d{{1,3}}).*?(?:。|；|;|\n|$)",
            text,
            re.DOTALL,
        )
        if match:
            aff = max(0, min(100, int(match.group(1))))
            neg = max(0, min(100, int(match.group(2))))
            comment = truncate_text(match.group(0), 160)
        else:
            aff = affirmative_score
            neg = negative_score
            comment = "评委未按结构化格式单列该项，已依据完整自然语言评语与总分进行兜底归纳。"
        scoring_breakdown.append(
            {
                "criterion": criterion,
                "affirmative_score": aff,
                "negative_score": neg,
                "comment": comment,
            },
        )
    stage_evaluations = []
    for stage in stage_names:
        stage_comment = first_match(
            [
                rf"{stage}[：:](.*?)(?=陈词一[：:]|质询一[：:]|陈词二[：:]|质询二[：:]|质询小结[：:]|自由辩[：:]|总结[：:]|$)",
                rf"{stage}(.*?)(?=\n\n|$)",
            ],
            text,
            "",
        )
        stage_evaluations.append(
            {
                "stage": stage,
                "affirmative_performance": "详见评委完整投票理由与该阶段比赛记录。",
                "negative_performance": "详见评委完整投票理由与该阶段比赛记录。",
                "judge_comment": truncate_text(stage_comment or text, 180),
            },
        )
    fallback = {
        "judge_name": judge_name,
        "focus": first_match([r"侧重点[:：](.*?)(?:\n|$)"], text, "按本评委设定维度进行综合评价"),
        "judging_standard": first_match([r"裁判标准[:：](.*?)(?:\n|$)", r"胜负标准[:：](.*?)(?:\n|$)"], text, "比较双方在论证建构、攻防回应、赛段推进与裁判说服上的综合表现"),
        "affirmative_score": affirmative_score,
        "negative_score": negative_score,
        "winner": winner,
        "ballot_reason": truncate_text(text, 1200),
        "scoring_breakdown": scoring_breakdown,
        "stage_evaluations": stage_evaluations,
        "key_clashes": [
            first_match([r"关键交锋[:：](.*?)(?:\n|$)"], text, "使用与依赖的界限"),
            "生成式 AI 与传统学习工具的差异",
            "监管可行性与学术诚信风险",
        ],
        "affirmative_best_point": first_match([r"正方(?:最佳论点|亮点)[:：](.*?)(?:\n|$)"], text, "正方强调工具中性与学习效率提升"),
        "negative_best_point": first_match([r"反方(?:最佳论点|亮点)[:：](.*?)(?:\n|$)"], text, "反方强调认知外包、依赖风险与学习真实性"),
        "affirmative_weakness": first_match([r"正方(?:问题|劣势|主要问题)[:：](.*?)(?:\n|$)"], text, "正方对广泛使用后的现实滥用与监管成本回应不足"),
        "negative_weakness": first_match([r"反方(?:问题|劣势|主要问题)[:：](.*?)(?:\n|$)"], text, "反方需避免把工具使用直接等同于依赖成瘾"),
        "decisive_clash": first_match([r"最关键.*?[:：](.*?)(?:\n|$)", r"关键交锋[:：](.*?)(?:\n|$)"], text, "双方围绕广泛使用是否必然导致依赖与能力退化形成核心交锋"),
        "advice_affirmative": "进一步给出可执行的使用边界、评价机制和防滥用方案，降低理想化色彩。",
        "advice_negative": "进一步证明广泛使用与能力退化之间的必然机制，避免只依赖风险想象。",
        "natural_commentary": text,
    }
    return JudgeDecision.model_validate(fallback).model_dump()


def format_judge_result(result: dict) -> str:
    if result.get("natural_commentary"):
        return str(result["natural_commentary"]).strip()
    lines = [
        f"评委：{result.get('judge_name', '')}",
        f"侧重点：{result.get('focus', '')}",
        f"裁判标准：{result.get('judging_standard', '')}",
        f"正方得分：{result.get('affirmative_score', '')}",
        f"反方得分：{result.get('negative_score', '')}",
        f"胜方：{result.get('winner', '')}",
        f"投票理由：{result.get('ballot_reason', '')}",
        "",
        "逐项评分：",
        *[
            f"- {item.get('criterion', '')}：正方 {item.get('affirmative_score', '')}，反方 {item.get('negative_score', '')}。{item.get('comment', '')}"
            for item in result.get("scoring_breakdown", [])
        ],
        "",
        "逐阶段点评：",
        *[
            f"- {item.get('stage', '')}：正方：{item.get('affirmative_performance', '')}；反方：{item.get('negative_performance', '')}；点评：{item.get('judge_comment', '')}"
            for item in result.get("stage_evaluations", [])
        ],
        "",
        "关键交锋：",
        *[f"- {item}" for item in result.get("key_clashes", [])],
        "",
        f"正方亮点：{result.get('affirmative_best_point', '')}",
        f"反方亮点：{result.get('negative_best_point', '')}",
        f"正方问题：{result.get('affirmative_weakness', '')}",
        f"反方问题：{result.get('negative_weakness', '')}",
        f"关键交锋：{result.get('decisive_clash', '')}",
        f"给正方建议：{result.get('advice_affirmative', '')}",
        f"给反方建议：{result.get('advice_negative', '')}",
    ]
    return "\n".join(lines)


def as_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def print_final_summary(results: list[dict]) -> dict:
    if not results:
        return {}
    aff_scores = [as_int(item.get("affirmative_score")) for item in results]
    neg_scores = [as_int(item.get("negative_score")) for item in results]
    aff_avg = sum(aff_scores) / len(aff_scores)
    neg_avg = sum(neg_scores) / len(neg_scores)
    votes = {"正方": 0, "反方": 0, "平局": 0}
    for item in results:
        winner = str(item.get("winner") or "平局")
        votes[winner if winner in votes else "平局"] += 1
    if votes["正方"] > votes["反方"]:
        final_winner = "正方"
    elif votes["反方"] > votes["正方"]:
        final_winner = "反方"
    elif aff_avg > neg_avg:
        final_winner = "正方"
    elif neg_avg > aff_avg:
        final_winner = "反方"
    else:
        final_winner = "平局"
    summary = dedent(
        f"""
        三评委投票：正方 {votes['正方']}，反方 {votes['反方']}，平局 {votes['平局']}
        平均分：正方 {aff_avg:.1f}，反方 {neg_avg:.1f}
        综合结果：{final_winner}
        """
    ).strip()
    print_block("最终结果", summary)
    return {
        "votes": votes,
        "affirmative_average": aff_avg,
        "negative_average": neg_avg,
        "final_winner": final_winner,
        "text": summary,
    }


def build_judge_digest(results: list[dict]) -> str:
    lines = []
    for result in results:
        commentary = str(result.get("natural_commentary") or result.get("ballot_reason") or "")
        lines.append(
            dedent(
                f"""
                {result.get('judge_name', '评委')}：裁决 {result.get('winner', '平局')}，正方 {result.get('affirmative_score', '')}，反方 {result.get('negative_score', '')}。
                {truncate_text(commentary, 1000)}
                """
            ).strip()
        )
    return "\n\n".join(lines)


async def generate_final_report(
    topic: str,
    affirmative_position: str,
    negative_position: str,
    transcript: str,
    results: list[dict],
    final_summary: dict,
    model_name: str,
    api_key: str,
    base_url: str,
    stream: bool,
) -> None:
    reporter = create_reporter(model_name, api_key, base_url, stream)
    msg = await reporter(
        Msg(
            "主持人",
            dedent(
                f"""
                请根据以下辩论记录、评委裁决和最终结果，生成一篇可以直接发布到知乎和小红书的观点文案。

                文案目标：
                1. 不要复盘比赛流程，不要说“本场比赛核心情况是……”；读者只需要看到一个有态度、有洞察的观点。
                2. 可以偏颇，优先站在最终胜方或更有现实解释力的一方，把另一方最强攻防转化为文章里的反面压力或必要提醒。
                3. 把比赛数据自然嵌入文案，例如“三位评委怎么投”“平均分差距”“综合结果”，让数据变成观点背书，而不是结尾报账。
                4. 把辩论中的精华攻防、关键比喻、现实场景、自由辩追问和评委胜负手揉进文章，不要机械引用。
                5. 结构适合传播：给出 3 个备选标题；正文开头要有钩子；中段要有现实场景、冲突和洞察；结尾要有金句和评论区互动引导。
                6. 文字可以更精彩、更锋利、更像人写的内容，但不能编造比赛之外的数据、政策、论文、新闻或评委没说过的事实。
                7. 默认输出中文，适合知乎长回答和小红书图文双平台发布。

                辩题：{topic}
                正方立场：{affirmative_position}
                反方立场：{negative_position}

                最终结果：
                {final_summary.get('text', '')}

                评委裁决摘要：
                {build_judge_digest(results)}

                完整辩论记录：
                {transcript}
                """
            ).strip(),
            "user",
        )
    )
    print_block("最终发布文案｜知乎 / 小红书", msg_text(msg))


async def run_debate(args: argparse.Namespace) -> None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(f"缺少 OPENAI_API_KEY。请在 {ENV_FILE} 中配置。")
    if not args.base_url:
        raise RuntimeError(f"缺少 OPENAI_BASE_URL。请在 {ENV_FILE} 中配置。")

    if not args.no_studio:
        agentscope.init(studio_url=args.studio_url)

    args.topic = resolve_topic(args.topic)
    print_block("本场辩题", args.topic)
    affirmative_position = args.affirmative or f"支持辩题：{args.topic}"
    negative_position = args.negative or f"反对辩题：{args.topic}"
    stream = not args.no_stream

    affirmative_team = {
        role: create_debater(f"正方{role}", "正方", affirmative_position, args.topic, args.model, api_key, args.base_url, stream)
        for role in ("一辩", "二辩", "三辩", "四辩")
    }
    negative_team = {
        role: create_debater(f"反方{role}", "反方", negative_position, args.topic, args.model, api_key, args.base_url, stream)
        for role in ("一辩", "二辩", "三辩", "四辩")
    }
    participants = list(affirmative_team.values()) + list(negative_team.values())

    turns: list[DebateTurn] = []
    context = DebateContext(args.topic, affirmative_position, negative_position)

    async with MsgHub(participants=participants):
        await speak(affirmative_team["一辩"], "陈词一", statement_instruction("正方", "一辩", 1, args.max_words), turns, context)
        await speak(negative_team["四辩"], "质询一", cross_exam_question_instruction("反方", "四辩", "正方一辩", args.cross_words), turns, context)
        await speak(affirmative_team["一辩"], "答询一", cross_exam_answer_instruction("正方", "一辩", "反方四辩", args.cross_words), turns, context)

        await speak(negative_team["一辩"], "陈词一", statement_instruction("反方", "一辩", 1, args.max_words), turns, context)
        await speak(affirmative_team["四辩"], "质询一", cross_exam_question_instruction("正方", "四辩", "反方一辩", args.cross_words), turns, context)
        await speak(negative_team["一辩"], "答询一", cross_exam_answer_instruction("反方", "一辩", "正方四辩", args.cross_words), turns, context)

        await speak(affirmative_team["二辩"], "陈词二", statement_instruction("正方", "二辩", 2, args.max_words), turns, context)
        await speak(negative_team["三辩"], "质询二", cross_exam_question_instruction("反方", "三辩", "正方二辩", args.cross_words), turns, context)
        await speak(affirmative_team["二辩"], "答询二", cross_exam_answer_instruction("正方", "二辩", "反方三辩", args.cross_words), turns, context)

        await speak(negative_team["二辩"], "陈词二", statement_instruction("反方", "二辩", 2, args.max_words), turns, context)
        await speak(affirmative_team["三辩"], "质询二", cross_exam_question_instruction("正方", "三辩", "反方二辩", args.cross_words), turns, context)
        await speak(negative_team["二辩"], "答询二", cross_exam_answer_instruction("反方", "二辩", "正方三辩", args.cross_words), turns, context)

        await speak(negative_team["三辩"], "质询小结", inquiry_summary_instruction("反方", "三辩", args.inquiry_summary_words), turns, context)
        await speak(affirmative_team["三辩"], "质询小结", inquiry_summary_instruction("正方", "三辩", args.inquiry_summary_words), turns, context)

        affirmative_free_roles = ("二辩", "三辩", "一辩", "四辩")
        negative_free_roles = ("二辩", "三辩", "一辩", "四辩")
        for round_index in range(1, args.free_rounds + 1):
            aff_role = affirmative_free_roles[(round_index - 1) % len(affirmative_free_roles)]
            neg_role = negative_free_roles[(round_index - 1) % len(negative_free_roles)]
            await speak(
                affirmative_team[aff_role],
                "自由辩论",
                free_debate_instruction("正方", round_index, args.free_words, is_first=round_index == 1),
                turns,
                context,
            )
            await speak(negative_team[neg_role], "自由辩论", free_debate_instruction("反方", round_index, args.free_words), turns, context)

        closing_order = (
            (negative_team["四辩"], "反方"),
            (affirmative_team["四辩"], "正方"),
        )
        if args.closing_first == "affirmative":
            closing_order = (
                (affirmative_team["四辩"], "正方"),
                (negative_team["四辩"], "反方"),
            )
        for agent, side in closing_order:
            await speak(agent, "总结陈词", closing_instruction(side, args.max_words), turns, context)

    transcript = build_transcript(args.topic, affirmative_position, negative_position, turns)

    judges = [
        create_judge(
            "情感共鸣评委",
            "重点看谁更能让普通听众感到问题与自己有关，谁更能建立价值认同、情境代入和表达感染力；但情绪必须有论证支撑。",
            args.model,
            api_key,
            args.base_url,
            stream,
        ),
        create_judge(
            "攻防技术评委",
            "重点看定义、标准、举证责任、反驳质量、追问压迫感、对关键交锋的回应和防守是否稳固。",
            args.model,
            api_key,
            args.base_url,
            stream,
        ),
        create_judge(
            "宏观事实规律评委",
            "重点看论证是否符合宏观事实、历史经验、制度约束、社会运行规律和因果机制，是否避免以个例替代趋势。",
            args.model,
            api_key,
            args.base_url,
            stream,
        ),
    ]

    results = []
    for judge in judges:
        results.append(await judge_debate(judge, transcript))
    final_summary = print_final_summary(results)
    if not args.no_report:
        await generate_final_report(
            args.topic,
            affirmative_position,
            negative_position,
            transcript,
            results,
            final_summary,
            args.model,
            api_key,
            args.base_url,
            stream,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="debate-agent", description="AgentScope 多智能体辩论工作流")
    parser.add_argument("--topic", default="")
    parser.add_argument("--affirmative", default="")
    parser.add_argument("--negative", default="")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL_NAME", "kimi-k2.5"))
    parser.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL", ""))
    parser.add_argument("--studio-url", default=os.getenv("AGENTSCOPE_STUDIO_URL", "http://localhost:3000"))
    parser.add_argument("--free-rounds", type=int, default=4)
    parser.add_argument("--max-words", type=int, default=420)
    parser.add_argument("--cross-words", type=int, default=220)
    parser.add_argument("--inquiry-summary-words", type=int, default=300)
    parser.add_argument("--free-words", type=int, default=260)
    parser.add_argument("--closing-first", choices=["affirmative", "negative"], default="negative")
    parser.add_argument("--no-report", action="store_true")
    parser.add_argument("--no-stream", action="store_true")
    parser.add_argument("--no-studio", action="store_true")
    return parser


def main() -> None:
    load_env_file(ENV_FILE)
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(run_debate(args))


if __name__ == "__main__":
    main()
