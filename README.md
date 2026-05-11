# hitsz-lifekit

`hitsz-lifekit` 是面向哈尔滨工业大学（深圳）学生的 Codex skills 仓库。

## 仓库结构

```text
hitsz-lifekit/
├── README.md
└── skills/
    └── hitsz-campusqa/
        ├── SKILL.md
        └── scripts/
            └── campusqa.py
```

约定：

- **Skill 目录**: `skills/<skill-name>/`
- **Skill 入口**: `skills/<skill-name>/SKILL.md`
- **脚本目录**: `skills/<skill-name>/scripts/`
- **根目录**: 只放仓库级说明，不放单个 skill 的重复文档

## 当前 skills

| Skill | 入口 | 用途 |
| --- | --- | --- |
| `hitsz-campusqa` | `skills/hitsz-campusqa/SKILL.md` | 回答哈工深校园事务问题，并给出 CampusQA 依据 |

具体实现、接口契约和回答规范请查看对应的 `SKILL.md`。

## 安装 skill

### 使用 skills-installer 安装

先安装 CLI：

```bash
npm install -g skills-installer
```

再安装本仓库中的 skill：

```text
skills-installer install https://github.com/guohuiyuan/hitsz-lifekit/tree/main/skills/hitsz-campusqa --client codex
```

如果安装后提示 `skills-installer: command not found`，可以直接用 `npx`：

```bash
npx skills-installer install https://github.com/guohuiyuan/hitsz-lifekit/tree/main/skills/hitsz-campusqa --client codex
```

### 本地复制安装

在本仓库根目录运行：

```bash
skills-installer install ./skills/hitsz-campusqa --client codex --project
```

复制后重启 Codex，或开启新的 Codex 会话。

## 最简调用示例

安装后，在 Codex 中直接点名已安装的 skill：

```text
使用已安装的 hitsz-campusqa skill 回答：校园卡丢了怎么办？请给出依据。
```

## 注意事项

- 不要在 skill 中硬编码账号、密码、Cookie 或 MFA 验证码。
- CampusQA 没有检索到答案时，不要编造校内政策或办事细节。
- 涉及政策、时间、费用、地点等信息时，应提示用户以学校最新通知或负责部门答复为准。
