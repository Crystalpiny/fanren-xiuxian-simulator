---
name: find-skill
description: 在本机查找、比对、安装 DSH 技能（skill）。用户说"找个 skill""有没有做 X 的技能""把这个技能装上/装到项目里"时用；也用于盘点技能目录、诊断技能为何没被加载。
---

# 找技能

用户要的是"能用的技能"，不是一份目录清单。先按下面的次序找，找到就把名字、能力、触发条件、所在路径报出来，并直接给出调用方式（`skill` 工具传技能名）。找不到就明说没有，再给出最接近的两三个候选，不要现编一个不存在的技能名。

## 一、先弄清技能是怎么被发现的

DSH 的技能由 `skill-filesystem` 提供方扫描固定根目录得到。**只认这些位置**，别处放的文件不会被加载，也不会出现在技能目录里。

| 优先级 | 根目录 | 来源标记 | 说明 |
|---|---|---|---|
| 1 | `<项目根>/.dsh/skills/` | project-dsh | 随项目走，可入库共享 |
| 2 | `<项目根>/.agents/skills/` | project-agents | 项目内兼容其他 agent 的旧位置 |
| 3 | `$DSH_HOME/skills/`（默认 `~/.dsh/skills/`） | user-dsh | 本机全局，跨项目可用；`.system/` 子目录被跳过 |
| 4 | `$DSH_AGENTS_HOME/skills/`（默认 `~/.agents/skills/`） | user-agents | 全局兼容位置，通常不存在 |
| 5 | 内置技能根（`$DSH_BUNDLED_SKILL_DIR`） | bundled | 随程序发行的技能，如 `skill-office`、`diagnose-windows-sandbox-acl` |

项目根按会话工作目录解析，可能上溯到 git 根。`customSkillDirs` 若配置过，插在第 2 与第 3 之间。

**每层认两种形态**，二者等价：

- **目录包**：`<根>/<技能名>/SKILL.md`，附带资源可放同目录（如 `references/`、`scripts/`），正文用相对路径引用；
- **单文件**：`<根>/<技能名>.md`。

**常见坑**：`.cursor/skills/`、`.claude/skills/`、`docs/skills/` 都不是扫描根。放在那里的技能不会被加载。遇到"我明明写了技能却调不出来"，先核对该技能是否落在这五个根之一。

## 二、怎么搜

用 `glob` 枚举、`grep` 读元数据，不要一次性读完正文。

```text
枚举全部技能定义：
  glob: **/SKILL.md（在 ~/.dsh、项目根、~/.agents 分别跑）
  glob: .dsh/skills/*.md（单文件形态）

按需求语义筛：
  grep: pattern="description:" path="C:\Users\<用户>\.dsh\skills" include="SKILL.md"
  grep: pattern="<关键词>" path=<技能根> include="*.md"

读候选技能：read 只读前 30 行，拿到 name/description/whenToUse 与正文要点即可
```

搜索顺序按上表优先级；同名技能按优先级判定谁生效（高优先级覆盖低优先级，低优先级条目仍可见但会被遮蔽）。**报结果时必须写清路径**，用户下次才知道该改哪一份。

## 三、判定"这个技能能不能用"

读候选文件的 frontmatter，逐项核：

| 字段 | 要求 | 缺失后果 |
|---|---|---|
| `name` | 必填，kebab-case（小写字母、数字、连字符） | 文件被整份忽略 |
| `description` | 必填，一句话说清"做什么+什么时候用" | 文件被整份忽略 |
| `whenToUse` | 可选，补充触发场景 | 无 |
| `disable-model-invocation: true` | 可选，模型不能主动调用，只能用户用 `/技能名` 触发 | 无 |
| `user-invocable: false` | 可选，只能模型调用，不出现在用户的斜杠命令里 | 无 |

其他约定：

- frontmatter 必须是文件开头的 `---` YAML 块，缺了整份忽略；
- 旧式驼峰键（`disableModelInvocation`、`userInvocable`）已废弃，写了会被判无效并忽略；
- 正文里不要复述 frontmatter 的 description，直接从怎么做讲起；
- 调用方式只有一个：`skill` 工具传 `name` 的精确值。

## 四、装到哪

| 用户意图 | 落点 |
|---|---|
| 只在这个项目用、要跟代码一起提交 | `<项目根>/.dsh/skills/<名>/SKILL.md` |
| 本机所有项目都能用 | `~/.dsh/skills/<名>/SKILL.md` |
| 只是想要一份别人给的技能文件 | 先 `glob` 找到源文件，再决定上面两者之一，复制而不是移动 |

用户没说位置时按这个默认：**通用工程类进全局，项目专属规则进项目**。装完必须报出最终绝对路径，并说明生效范围。

**目录外写入的沙箱提示**：装到 `~/.dsh/skills/` 属于工作目录之外的写入。优先用 `write` 工具直接写；若被判沙箱拒绝，按沙箱提示走一次一次性提权重试，并在回复里告诉用户这次为什么需要写到家目录，不要偷偷换个位置了事。

**装完的生效时机**：`skill-filesystem` 会监听这些根目录，新增技能通常在本回合后即可被 `skill` 工具查到，无需重启；但**技能目录清单（会话提示里的 available skills）要等目录变化事件重新下发才会更新**。装完可以立刻用 `skill` 工具按名字试调一次，能调入即算成功；若查不到，再核对路径、frontmatter 与文件名是否与 `name` 一致。

## 五、交付格式

给用户的答复按这个骨架，不要贴整份技能正文：

```text
找到 / 没找到：<技能名>
能力：<一句话>
触发：<什么话、什么任务会用到>
路径：<绝对路径>
状态：<可用 / frontmatter 缺 description 被忽略 / 不在扫描根内，不会加载>
怎么用：调用 skill 工具传 name="<技能名>"（或用户输入 /<技能名>）
```

只有用户明确要求"看看里面写了什么"时，才展示技能正文。
