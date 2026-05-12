# debate-agent

基于 AgentScope 的多智能体辩论工作流。

## 流程

1. **陈词一**: 双方一辩完成定义、标准和主论证架构。
2. **质询一 / 答询一**: 双方四辩分别质询对方一辩，被质询方正面答询。
3. **陈词二**: 双方二辩补强论证并处理前期攻防。
4. **质询二 / 答询二**: 双方三辩分别质询对方二辩，被质询方正面答询。
5. **质询小结**: 双方三辩总结质询收益。
6. **自由辩论**: 自然语言一问一答接力，必须先回答对方上一问，再顺着同一交锋点反打并自然回抛一个新问题。
7. **总结陈词**: 双方四辩总结胜负标准和最终投票理由。
8. **评委裁决**: 三位评委分别从不同维度给出自然语言长评、比分和裁决。
9. **最终发布文案**: 自动生成一篇可偏颇、有观点、融合比赛数据和精华攻防的知乎 / 小红书文案。

## 三位评委

- **情感共鸣评委**: 关注价值认同、情境代入、表达感染力。
- **攻防技术评委**: 关注定义、标准、反驳、追问、防守和关键交锋。
- **宏观事实规律评委**: 关注宏观事实、社会规律、制度约束和因果机制。

## 安装依赖

```bash
pip install -r agents/debate-agent/requirements.txt
```

## 配置 `.env`

复制示例文件：

```bash
cp agents/debate-agent/.env.example agents/debate-agent/.env
```

然后填写 OpenAI-compatible API 配置：

```env
OPENAI_API_KEY=your_openai_compatible_api_key_here
OPENAI_BASE_URL=https://api.example.com/v1
OPENAI_MODEL_NAME=kimi-k2.5
AGENTSCOPE_STUDIO_URL=http://localhost:3000
```

`.env` 已被根目录 `.gitignore` 忽略，不要提交真实密钥。

## 启动 AgentScope Studio

```bash
npm install -g @agentscope/studio
as_studio
```

脚本默认执行：

```python
agentscope.init(studio_url="http://localhost:3000")
```

运行后可以在 AgentScope Studio 中查看应用轨迹、模型调用和多智能体交互记录。

## 运行

```bash
python agents/debate-agent/main.py --topic "大学生是否应该广泛使用生成式 AI 辅助学习？"
```

也可以直接运行后按提示输入辩题：

```bash
python agents/debate-agent/main.py
```

自定义正反方立场：

```bash
python agents/debate-agent/main.py \
  --topic "大学生是否应该广泛使用生成式 AI 辅助学习？" \
  --affirmative "大学生应该广泛使用生成式 AI 辅助学习" \
  --negative "大学生不应该广泛使用生成式 AI 辅助学习"
```

如果暂时不连接 Studio：

```bash
python agents/debate-agent/main.py --no-studio
```

## 常用参数

- `--topic`: 辩题。
- 不传 `--topic` 时，会在终端提示用户输入辩题；直接回车则使用默认辩题。
- `--affirmative`: 正方立场，默认自动生成为支持辩题。
- `--negative`: 反方立场，默认自动生成为反对辩题。
- `--model`: OpenAI-compatible 模型名，默认读取 `OPENAI_MODEL_NAME`，否则使用 `kimi-k2.5`。
- `--base-url`: OpenAI-compatible API 地址，默认读取 `OPENAI_BASE_URL`。
- `--studio-url`: AgentScope Studio 地址，默认读取 `AGENTSCOPE_STUDIO_URL`，否则使用 `http://localhost:3000`。
- `--free-rounds`: 自由辩论接力轮数，默认 `4`。
- `--max-words`: 陈词、总结陈词单次发言字数上限，默认 `420`。
- `--cross-words`: 质询、答询单次发言字数上限，默认 `220`。
- `--inquiry-summary-words`: 质询小结单次发言字数上限，默认 `300`。
- `--free-words`: 自由辩论单次发言字数上限，默认 `260`。
- `--closing-first`: 总结陈词先后手，默认 `negative`。
- `--no-report`: 跳过最后的知乎 / 小红书发布文案。
- `--no-stream`: 关闭流式输出。
- `--no-studio`: 不注册到 AgentScope Studio。
