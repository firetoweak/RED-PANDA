import {
  ActionIcon, Alert, Button, Group, Menu, Skeleton, Stack,
  Text, Textarea, Tooltip, useMantineColorScheme,
} from "@mantine/core";
import {
  IconArrowUp, IconArrowUpRight, IconCheck, IconChevronDown, IconMessageCircle, IconMoon,
  IconPlayerStopFilled, IconPlus, IconSettings, IconSun,
} from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";
import { ModelSettingsDrawer } from "./ModelSettingsDrawer";
import { ModelPicker, type Selection } from "./ModelPicker";
import { effortName, previewApi, providerName, type CatalogModel, type ModelPreviewApi, type Preferences } from "./api";
import brandMark from "../../../assets/brand/konden-mark.svg";

type Message = { id: number; role: "user" | "assistant"; text: string; model?: string; effort?: string | null };
type Bot = { id: number; name: string; draft: string; selection: Selection; messages: Message[] };
type InFlight = { bot: number; selection: Selection; controller: AbortController };

export function ModelPreview({ api = previewApi }: { api?: ModelPreviewApi }) {
  const [models, setModels] = useState<CatalogModel[]>([]);
  const [preferences, setPreferences] = useState<Preferences | null>(null);
  const [bots, setBots] = useState<Bot[]>([]);
  const [activeId, setActiveId] = useState(1);
  const [settingsOpened, setSettingsOpened] = useState(window.location.hash === "#settings");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [replyError, setReplyError] = useState<string | null>(null);
  const [inFlight, setInFlight] = useState<InFlight | null>(null);
  const [botSearch, setBotSearch] = useState("");
  const { colorScheme, toggleColorScheme } = useMantineColorScheme();
  const timeline = useRef<HTMLDivElement>(null);
  const request = useRef<InFlight | null>(null);
  const nextMessageId = useRef(1);

  useEffect(() => {
    let mounted = true;
    Promise.all([api.loadModels(), api.loadPreferences()]).then(([catalog, value]) => {
      if (!mounted) return;
      setModels(catalog); setPreferences(value);
      setBots([{ id: 1, name: "UI 体验", draft: "", messages: [], selection: {
        model: value.defaultModel, effort: value.favorites[value.defaultModel].effort,
      } }]);
    }, (cause: unknown) => { if (mounted) setLoadError(cause instanceof Error ? cause.message : String(cause)); });
    return () => { mounted = false; request.current?.controller.abort(); };
  }, [api]);
  const active = bots.find((bot) => bot.id === activeId);
  useEffect(() => { timeline.current?.scrollTo({ top: timeline.current.scrollHeight, behavior: "smooth" }); }, [activeId, active?.messages.length, inFlight]);

  function updateBot(id: number, change: (value: Bot) => Bot) {
    setBots((current) => current.map((bot) => bot.id === id ? change(bot) : bot));
  }
  function newBot() {
    const id = Math.max(...bots.map((bot) => bot.id)) + 1;
    setBots((current) => [...current, { id, name: `体验 Bot ${id}`, draft: "", messages: [],
      selection: { model: preferences!.defaultModel, effort: preferences!.favorites[preferences!.defaultModel].effort } }]);
    setActiveId(id); setReplyError(null);
  }
  async function send() {
    if (!active?.draft.trim() || inFlight) return;
    const text = active.draft.trim();
    const current: InFlight = { bot: active.id, selection: { ...active.selection }, controller: new AbortController() };
    const item = models.find((model) => model.id === current.selection.model)!;
    updateBot(active.id, (bot) => ({ ...bot, draft: "", messages: [...bot.messages, { id: nextMessageId.current++, role: "user", text }] }));
    setReplyError(null); setInFlight(current); request.current = current;
    try {
      const reply = await api.reply(text, item, current.selection.effort, current.controller.signal);
      updateBot(current.bot, (bot) => ({ ...bot, messages: [...bot.messages, {
        id: nextMessageId.current++, role: "assistant", text: reply, model: item.id, effort: current.selection.effort,
      }] }));
    } catch (cause) {
      if (!(cause instanceof DOMException && cause.name === "AbortError")) {
        setReplyError(cause instanceof Error ? cause.message : String(cause));
      }
    } finally { setInFlight(null); request.current = null; }
  }

  if (loadError) return <div className="preview-start"><Alert color="red" title="预览未能加载">{loadError}<Button mt="md" onClick={() => window.location.reload()}>重新加载</Button></Alert></div>;
  if (!preferences || !active) return <div className="preview-start"><Stack w={300}><Skeleton height={24} /><Skeleton height={80} /><Text c="dimmed" size="sm">正在准备模型配置预览…</Text></Stack></div>;
  const selected = models.find((item) => item.id === active.selection.model)!;
  const selectionPending = inFlight?.bot === active.id
    && (inFlight.selection.model !== active.selection.model || inFlight.selection.effort !== active.selection.effort);
  const botButtons = bots.filter((bot) => bot.name.includes(botSearch));

  return <div className="model-preview-root">
    <aside className="preview-sidebar" aria-label="Bot 名册">
      <div className="preview-sidebar-heading"><IconMessageCircle size={22} stroke={1.5} /><Tooltip label="新建体验 Bot"><ActionIcon variant="subtle" color="gray" aria-label="新建体验 Bot" onClick={newBot}><IconPlus size={20} /></ActionIcon></Tooltip></div>
      <div className="preview-sidebar-search"><input aria-label="搜索体验 Bot" placeholder="搜索对话" value={botSearch} onChange={(event) => setBotSearch(event.currentTarget.value)} /></div>
      <Text size="xs" c="dimmed" className="preview-roster-label">体验 Bot <span>{bots.length}</span></Text>
      <div className="preview-bot-list">{botButtons.map((bot) => <button type="button" key={bot.id}
        className={`preview-bot${bot.id === activeId ? " is-active" : ""}`} aria-current={bot.id === activeId ? "page" : undefined}
        onClick={() => { setActiveId(bot.id); setReplyError(null); }}><span className="preview-bot-avatar"><IconMessageCircle size={21} stroke={1.4} /></span>
        <span>{bot.name}</span>{inFlight?.bot === bot.id ? <span className="bot-working-dot" aria-label="正在回复" /> : null}</button>)}</div>
      {botSearch && !botButtons.length ? <Text size="sm" c="dimmed" p="sm">没有匹配的对话</Text> : null}
      <div className="preview-sidebar-footer">
        <Button variant="subtle" color="gray" justify="flex-start" leftSection={<IconSettings size={17} />} fullWidth onClick={() => setSettingsOpened(true)}>模型配置</Button>
        <div className="preview-footer-bottom"><Text size="xs" c="dimmed">交互预览 · 示例数据</Text><Tooltip label={colorScheme === "dark" ? "切换浅色" : "切换深色"}>
          <ActionIcon variant="subtle" color="gray" aria-label="切换外观" onClick={() => toggleColorScheme()}>{colorScheme === "dark" ? <IconSun size={17} /> : <IconMoon size={17} />}</ActionIcon>
        </Tooltip></div>
      </div>
    </aside>
    <main className="preview-chat">
      <header className="preview-chat-header"><div className="preview-chat-identity"><span className="preview-header-avatar"><IconMessageCircle size={20} stroke={1.4} /></span>
        <div><Text size="sm" fw={600}>{active.name}</Text><Text size="xs" c="dimmed">模型与推理强度交互预览</Text></div></div>
        <Group gap={5}><Menu withinPortal position="bottom-end"><Menu.Target><ActionIcon variant="subtle" color="gray" aria-label="切换体验 Bot" className="preview-mobile-bots"><IconChevronDown size={18} /></ActionIcon></Menu.Target>
          <Menu.Dropdown>{bots.map((bot) => <Menu.Item key={bot.id} onClick={() => setActiveId(bot.id)} rightSection={bot.id === activeId ? <IconCheck size={14} /> : null}>{bot.name}</Menu.Item>)}<Menu.Divider /><Menu.Item onClick={newBot}>新建体验 Bot</Menu.Item></Menu.Dropdown></Menu>
          <Tooltip label="模型配置"><ActionIcon aria-label="打开模型配置" variant="subtle" color="gray" size="lg" onClick={() => setSettingsOpened(true)}><IconSettings size={18} /></ActionIcon></Tooltip></Group>
      </header>
      <div className="preview-timeline" ref={timeline}>
        {active.messages.length === 0 ? <div className="preview-welcome">
          <img src={brandMark} alt="konden" width={60} height={60} />
          <h1>今天想一起完成什么？</h1>
          <Text c="dimmed" size="sm">从一个问题开始，按需要选择模型和推理强度。</Text>
          <button type="button" className="welcome-settings-link" onClick={() => setSettingsOpened(true)}>先挑选常用模型 <IconArrowUpRight size={14} /></button>
        </div> : <div className="preview-message-list">{active.messages.map((message) => <article key={message.id}
          className={`preview-message is-${message.role}`} aria-label={message.role === "user" ? "你的消息" : "示例回复"}>
          {message.role === "assistant" ? <div className="preview-message-avatar"><IconMessageCircle size={17} /></div> : null}
          <div><p>{message.text}</p>{message.model ? <Text size="xs" c="dimmed" mt={8}>{providerName(models.find((item) => item.id === message.model)!.provider)} · {models.find((item) => item.id === message.model)!.name}
            {models.find((item) => item.id === message.model)!.efforts.length ? ` · ${effortName(models.find((item) => item.id === message.model)!, message.effort ?? null)}` : ""} · 示例回复</Text> : null}</div>
        </article>)}</div>}
        {inFlight?.bot === active.id ? <div className="preview-reply-progress" role="status"><span className="thinking-dots"><i /><i /><i /></span><Text size="sm" c="dimmed">正在生成示例回复</Text></div> : null}
      </div>
      <div className="preview-composer-area">
        {replyError ? <Alert title="回复未能完成" color="red" mb="sm">{replyError}</Alert> : null}
        <form className="preview-composer" onSubmit={(event) => { event.preventDefault(); void send(); }}>
          <Textarea aria-label="消息" placeholder="输入消息，体验模型与强度切换…" value={active.draft}
            onChange={(event) => { const draft = event.currentTarget.value; updateBot(activeId, (bot) => ({ ...bot, draft })); }}
            autosize minRows={2} maxRows={7} variant="unstyled"
            onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); } }} />
          <div className="preview-composer-toolbar"><ModelPicker models={models} preferences={preferences}
            selection={active.selection} onSelect={(selection) => updateBot(activeId, (bot) => ({ ...bot, selection }))}
            pending={selectionPending} />
            {inFlight?.bot === active.id ? <ActionIcon aria-label="停止示例回复" type="button" radius="xl" variant="filled" color="gray" size={34} onClick={() => inFlight.controller.abort()}><IconPlayerStopFilled size={16} /></ActionIcon>
              : <ActionIcon aria-label="发送" type="submit" radius="xl" variant="filled" color="gray" size={34} disabled={!active.draft.trim() || inFlight !== null}><IconArrowUp size={19} /></ActionIcon>}
          </div>
        </form>
        <Text className="preview-disclaimer" size="xs" c="dimmed">此页面使用示例目录和回复，偏好独立保存在本机。</Text>
      </div>
    </main>
    <ModelSettingsDrawer opened={settingsOpened} onClose={() => setSettingsOpened(false)} preferences={preferences}
      allModels={models} onSaved={setPreferences} api={api} />
  </div>;
}
