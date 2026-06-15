import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  CalendarDays,
  CheckCircle2,
  Clock3,
  ExternalLink,
  LogOut,
  Plus,
  Search,
  Settings,
  ShieldCheck,
  AlertCircle,
  Trash2,
  Trees,
} from 'lucide-react';
import {
  User,
  onAuthStateChanged,
  signInWithPopup,
  signOut,
} from 'firebase/auth';
import { auth, googleProvider, isFirebaseConfigured } from './firebase';
import './styles.css';

type Tab = 'watch' | 'settings' | 'results';

type WatchCampground = {
  provider?: string | null;
  park_name?: string | null;
  campground_name: string;
  campground_id?: string | null;
  park_id?: string | number | null;
};

type MonitorSettings = {
  date_mode: 'relative' | 'range';
  lookahead_amount: number;
  lookahead_unit: 'weeks' | 'months';
  start_date: string | null;
  end_date: string | null;
  stay_nights: number;
  require_weekend_or_holiday: boolean;
  schedule_enabled: boolean;
  query_interval_hours: number;
  openai_model?: string | null;
};

type AppConfig = {
  watched_campgrounds: WatchCampground[];
  settings: MonitorSettings;
};

type SearchCandidate = WatchCampground & {
  provider: string;
  park_name: string;
  campground_id: string;
};

type Opening = {
  park_name?: string;
  campground_name: string;
  provider?: string;
  site: string;
  stay_dates?: string;
  date?: string;
  day_name?: string;
  day_type?: string;
  nights: number;
  url: string;
  is_new?: boolean;
};

type StatusPayload = {
  status: string;
  user_email: string;
  github_configured: boolean;
  openai_configured: boolean;
  generated_at_display?: string | null;
  current_openings_count: number;
  new_openings_count: number;
};

type ResultsPayload = {
  generated_at_display?: string;
  current_openings_count: number;
  new_openings_count: number;
  current_openings?: Opening[];
  new_openings?: Opening[];
};

type WorkflowRun = {
  id?: number;
  run_number?: number;
  status?: string;
  conclusion?: string | null;
  html_url?: string;
  updated_at?: string;
};

type WorkflowStatusPayload = {
  status: string;
  conclusion?: string | null;
  run?: WorkflowRun | null;
};

type WorkflowUiState = {
  tone: 'idle' | 'running' | 'success' | 'error';
  title: string;
  detail?: string;
  url?: string;
};

const defaultConfig: AppConfig = {
  watched_campgrounds: [],
  settings: {
    date_mode: 'relative',
    lookahead_amount: 6,
    lookahead_unit: 'months',
    start_date: null,
    end_date: null,
    stay_nights: 2,
    require_weekend_or_holiday: false,
    schedule_enabled: true,
    query_interval_hours: 2,
    openai_model: 'gpt-5.4-mini',
  },
};

function allowedEmails(): string[] {
  return String(import.meta.env.VITE_ALLOWED_EMAILS || '')
    .split(',')
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean);
}

function normalizeCampgroundName(value?: string | null): string {
  return String(value || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .split(' ')
    .filter(
      (word) =>
        word &&
        !['campground', 'campgrounds', 'camp', 'campsite', 'campsites', 'yosemite', 'national', 'park'].includes(
          word,
        ),
    )
    .join(' ')
    .trim();
}

function isCandidateWatched(candidate: WatchCampground, watched: WatchCampground[]): boolean {
  const candidateProvider = candidate.provider?.toLowerCase();
  const candidateId = candidate.campground_id ? String(candidate.campground_id) : '';
  const candidateName = normalizeCampgroundName(candidate.campground_name);

  return watched.some((item) => {
    const itemProvider = item.provider?.toLowerCase();
    const itemId = item.campground_id ? String(item.campground_id) : '';
    if (candidateProvider && itemProvider && candidateId && itemId) {
      return candidateProvider === itemProvider && candidateId === itemId;
    }

    const itemName = normalizeCampgroundName(item.campground_name);
    return Boolean(
      candidateName &&
        itemName &&
        (candidateName === itemName || candidateName.includes(itemName) || itemName.includes(candidateName)),
    );
  });
}

function App() {
  const [user, setUser] = useState<User | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [tab, setTab] = useState<Tab>('watch');
  const [config, setConfig] = useState<AppConfig>(defaultConfig);
  const [status, setStatus] = useState<StatusPayload | null>(null);
  const [results, setResults] = useState<ResultsPayload | null>(null);
  const [query, setQuery] = useState('');
  const [candidates, setCandidates] = useState<SearchCandidate[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [hasLoadedInitialData, setHasLoadedInitialData] = useState(false);
  const [notice, setNotice] = useState('');
  const [pendingRemoval, setPendingRemoval] = useState<{ index: number; item: WatchCampground } | null>(null);
  const [workflowState, setWorkflowState] = useState<WorkflowUiState>({
    tone: 'idle',
    title: '尚未手动触发查询',
  });
  const allowlist = useMemo(() => allowedEmails(), []);

  const isAllowed = Boolean(
    user?.email && (allowlist.length === 0 || allowlist.includes(user.email.toLowerCase())),
  );

  const apiFetch = useCallback(
    async <T,>(path: string, init: RequestInit = {}): Promise<T> => {
      if (!user) throw new Error('Not signed in');
      const token = await user.getIdToken();
      const response = await fetch(path, {
        ...init,
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
          ...(init.headers || {}),
        },
      });
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || `Request failed: ${response.status}`);
      }
      return response.json() as Promise<T>;
    },
    [user],
  );

  const loadAll = useCallback(async () => {
    if (!user || !isAllowed) return;
    setBusy('loading');
    try {
      const [nextConfig, nextStatus, nextResults] = await Promise.all([
        apiFetch<AppConfig>('/api/config'),
        apiFetch<StatusPayload>('/api/status'),
        apiFetch<ResultsPayload>('/api/results'),
      ]);
      setConfig(nextConfig);
      setStatus(nextStatus);
      setResults(nextResults);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '加载失败');
    } finally {
      setHasLoadedInitialData(true);
      setBusy(null);
    }
  }, [apiFetch, isAllowed, user]);

  useEffect(() => {
    if (!auth) return;
    return onAuthStateChanged(auth, (nextUser) => {
      setUser(nextUser);
      setAuthReady(true);
    });
  }, []);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  async function handleSignIn() {
    if (!auth) return;
    await signInWithPopup(auth, googleProvider);
  }

  async function handleSignOut() {
    if (!auth) return;
    await signOut(auth);
  }

  async function handleSearch() {
    if (!query.trim()) return;
    setBusy('search');
    setNotice('');
    try {
      const payload = await apiFetch<{ candidates: SearchCandidate[] }>('/api/campgrounds/search', {
        method: 'POST',
        body: JSON.stringify({ query: query.trim() }),
      });
      setCandidates(payload.candidates || []);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '查找失败');
    } finally {
      setBusy(null);
    }
  }

  async function saveConfig(nextConfig: AppConfig, successMessage = '已保存') {
    setConfig(nextConfig);
    setBusy('save');
    setNotice('');
    try {
      const saved = await apiFetch<AppConfig>('/api/config', {
        method: 'PUT',
        body: JSON.stringify(nextConfig),
      });
      setConfig(saved);
      setNotice(successMessage);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '保存失败');
    } finally {
      setBusy(null);
    }
  }

  function addCandidate(candidate: SearchCandidate) {
    if (isCandidateWatched(candidate, config.watched_campgrounds)) {
      setNotice('这个营地已经在关注列表里');
      return;
    }
    void saveConfig(
      {
        ...config,
        watched_campgrounds: [...config.watched_campgrounds, candidate],
      },
      '已添加到关注列表',
    );
  }

  function requestRemoveCampground(index: number) {
    const item = config.watched_campgrounds[index];
    if (!item) return;
    setPendingRemoval({ index, item });
  }

  function confirmRemoveCampground() {
    if (!pendingRemoval) return;
    const { index } = pendingRemoval;
    setPendingRemoval(null);
    void saveConfig(
      {
        ...config,
        watched_campgrounds: config.watched_campgrounds.filter((_, itemIndex) => itemIndex !== index),
      },
      '已从关注列表删除',
    );
  }

  async function triggerWorkflow() {
    setBusy('workflow');
    setNotice('');
    setWorkflowState({
      tone: 'running',
      title: '正在启动 GitHub Actions 查询',
      detail: '已发送触发请求，正在等待 GitHub 创建运行记录。',
    });
    try {
      await apiFetch('/api/workflow/run', {
        method: 'POST',
        body: JSON.stringify({ ref: 'main' }),
      });
      setNotice('');
      setWorkflowState({
        tone: 'running',
        title: '正在查询',
        detail: 'GitHub Actions 已触发，正在等待运行结果。',
      });
      void pollWorkflowStatus();
    } catch (error) {
      setWorkflowState({
        tone: 'error',
        title: '查询触发失败',
        detail: error instanceof Error ? error.message : '触发失败',
      });
      setNotice(error instanceof Error ? error.message : '触发失败');
    } finally {
      setBusy(null);
    }
  }

  async function pollWorkflowStatus() {
    const maxAttempts = 120;
    const intervalMs = 10000;
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      await sleep(attempt === 0 ? 3000 : intervalMs);
      try {
        const workflow = await apiFetch<WorkflowStatusPayload>('/api/workflow/latest');
        const run = workflow.run;
        if (workflow.status === 'queued' || workflow.status === 'in_progress') {
          setWorkflowState({
            tone: 'running',
            title: workflow.status === 'queued' ? '查询排队中' : '正在查询',
            detail: run?.run_number ? `GitHub Actions #${run.run_number} 正在运行。` : '正在等待 GitHub Actions 完成。',
            url: run?.html_url,
          });
          continue;
        }
        if (workflow.status === 'completed') {
          if (workflow.conclusion === 'success') {
            const nextResults = await apiFetch<ResultsPayload>('/api/results');
            setResults(nextResults);
            const newCount = nextResults.new_openings_count || 0;
            const currentCount = nextResults.current_openings_count || 0;
            setWorkflowState({
              tone: 'success',
              title: newCount > 0 ? `查询完毕：发现 ${newCount} 个新可用营地` : '查询完毕：无新可用营地',
              detail:
                newCount > 0
                  ? '可以到查询结果页查看详情和预订链接。'
                  : currentCount > 0
                    ? `没有新增结果，当前仍有 ${currentCount} 个可用窗口。`
                    : '本次没有发现符合条件的可用营地。',
              url: run?.html_url,
            });
            await loadAll();
            return;
          }
          setWorkflowState({
            tone: 'error',
            title: '查询失败',
            detail: `GitHub Actions 结束状态：${workflow.conclusion || 'unknown'}。`,
            url: run?.html_url,
          });
          return;
        }
      } catch (error) {
        setWorkflowState({
          tone: 'error',
          title: '查询状态读取失败',
          detail: error instanceof Error ? error.message : '无法读取 GitHub Actions 状态。',
        });
        return;
      }
    }
    setWorkflowState({
      tone: 'error',
      title: '查询仍在进行',
      detail: '已停止自动刷新。GitHub Actions 可能仍在运行，请稍后到查询结果页刷新。',
    });
  }

  if (!isFirebaseConfigured) {
    return <SetupMissing />;
  }

  if (!authReady) {
    return <ShellState text="正在检查登录状态" />;
  }

  if (!user) {
    return <LoginScreen onSignIn={handleSignIn} />;
  }

  if (!isAllowed) {
    return <DeniedScreen email={user.email || ''} onSignOut={handleSignOut} />;
  }

  return (
    <main className="app-shell">
      <aside className="side-rail">
        <div className="brand-mark">
          <Trees size={28} />
          <div>
            <strong>Camp Watch</strong>
            <span>营地监控系统</span>
          </div>
        </div>
        <div className="status-chip">
          <CheckCircle2 size={18} />
          <div>
            <strong>服务在线</strong>
            <span>{status?.github_configured ? 'GitHub 已连接' : '等待 GitHub 配置'}</span>
          </div>
        </div>
        <div className="rail-bottom">
          <div className="mini-stat">
            <span>最近检查</span>
            <strong>{status?.generated_at_display || '暂无记录'}</strong>
          </div>
          <button className="ghost-button" onClick={handleSignOut}>
            <LogOut size={16} />
            退出
          </button>
        </div>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <nav className="tabs" aria-label="Main tabs">
            <button className={tab === 'watch' ? 'active' : ''} onClick={() => setTab('watch')}>
              营地关注
            </button>
            <button
              className={tab === 'settings' ? 'active' : ''}
              onClick={() => setTab('settings')}
            >
              查询条件
            </button>
            <button
              className={tab === 'results' ? 'active' : ''}
              onClick={() => setTab('results')}
            >
              查询结果
            </button>
          </nav>
          <div className="identity">
            <ShieldCheck size={16} />
            {user.email}
          </div>
        </header>

        <div className="content">
          {notice && <div className="notice">{notice}</div>}
          {!hasLoadedInitialData && <LoadingPanel text="正在加载配置和查询结果" />}
          {hasLoadedInitialData && tab === 'watch' && (
            <WatchTab
              busy={busy}
              query={query}
              setQuery={setQuery}
              candidates={candidates}
              watched={config.watched_campgrounds}
              onSearch={handleSearch}
              onAdd={addCandidate}
              onRemove={requestRemoveCampground}
            />
          )}
          {hasLoadedInitialData && tab === 'settings' && (
            <SettingsTab
              settings={config.settings}
              busy={busy}
              workflowState={workflowState}
              onChange={(settings) => setConfig({ ...config, settings })}
              onSave={() => saveConfig(config)}
              onRun={triggerWorkflow}
            />
          )}
          {hasLoadedInitialData && tab === 'results' && (
            <ResultsTab loading={busy === 'loading'} results={results} onRefresh={loadAll} />
          )}
        </div>
      </section>
      {pendingRemoval && (
        <ConfirmDeleteDialog
          item={pendingRemoval.item}
          busy={busy === 'save'}
          onCancel={() => setPendingRemoval(null)}
          onConfirm={confirmRemoveCampground}
        />
      )}
    </main>
  );
}

function LoadingPanel({ text }: { text: string }) {
  return (
    <section className="panel loading-panel" aria-live="polite">
      <div className="loading-spinner" />
      <strong>{text}</strong>
      <span>正在从后端同步 GitHub Actions 配置和最近一次报告。</span>
    </section>
  );
}

function ConfirmDeleteDialog(props: {
  item: WatchCampground;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={props.onCancel}>
      <section
        className="confirm-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-delete-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="dialog-icon">
          <AlertCircle size={22} />
        </div>
        <div>
          <span>确认删除</span>
          <h2 id="confirm-delete-title">从关注列表删除这个营地？</h2>
          <p>
            删除后，后续定时查询不会再检查 <strong>{props.item.campground_name}</strong>。
          </p>
        </div>
        <div className="dialog-actions">
          <button className="line-button" onClick={props.onCancel} disabled={props.busy}>
            取消
          </button>
          <button className="danger-button solid" onClick={props.onConfirm} disabled={props.busy}>
            <Trash2 size={16} />
            确认删除
          </button>
        </div>
      </section>
    </div>
  );
}

function WatchTab(props: {
  busy: string | null;
  query: string;
  setQuery: (value: string) => void;
  candidates: SearchCandidate[];
  watched: WatchCampground[];
  onSearch: () => void;
  onAdd: (candidate: SearchCandidate) => void;
  onRemove: (index: number) => void;
}) {
  return (
    <div className="single-column">
      <section className="panel">
        <div className="section-heading">
          <div>
            <span>AI 只从真实候选中选择</span>
            <h1>查找营地</h1>
          </div>
        </div>
        <div className="search-line">
          <Search size={20} />
          <input
            value={props.query}
            onChange={(event) => props.setQuery(event.target.value)}
            onKeyDown={(event) => event.key === 'Enter' && props.onSearch()}
            placeholder="输入营地名称"
          />
          <button className="primary-button" onClick={props.onSearch} disabled={props.busy === 'search'}>
            <Search size={16} />
            查找
          </button>
        </div>
        {props.candidates.length > 0 && (
          <div className="result-summary">
            显示 {props.candidates.length} 个候选，已在关注列表里的营地会标记为已关注。
          </div>
        )}
        <div className="simple-list candidate-results">
          {props.candidates.map((candidate) => {
            const alreadyWatched = isCandidateWatched(candidate, props.watched);
            return (
              <div className="candidate-row" key={`${candidate.provider}-${candidate.campground_id}`}>
                <div>
                  <strong>{candidate.campground_name}</strong>
                  <span>
                    {candidate.provider} · {candidate.park_name}
                  </span>
                </div>
                <button
                  className={alreadyWatched ? 'line-button watched-button' : 'line-button'}
                  disabled={alreadyWatched}
                  onClick={() => props.onAdd(candidate)}
                >
                  {alreadyWatched ? <CheckCircle2 size={16} /> : <Plus size={16} />}
                  {alreadyWatched ? '已关注' : '添加'}
                </button>
              </div>
            );
          })}
          {!props.candidates.length && <div className="empty-line">搜索后会在这里显示候选营地</div>}
        </div>
      </section>

      <section className="panel">
        <div className="section-heading inline">
          <div>
            <span>已关注 {props.watched.length} 个</span>
            <h2>正在关注</h2>
          </div>
        </div>
        <div className="simple-list">
          {props.watched.map((item, index) => (
            <div className="candidate-row" key={`${item.campground_name}-${index}`}>
              <div>
                <strong>{item.campground_name}</strong>
                <span>{[item.provider, item.park_name, item.campground_id].filter(Boolean).join(' · ')}</span>
              </div>
              <button className="danger-button" onClick={() => props.onRemove(index)} aria-label="删除">
                <Trash2 size={16} />
                删除
              </button>
            </div>
          ))}
          {!props.watched.length && <div className="empty-line">还没有关注营地</div>}
        </div>
      </section>
    </div>
  );
}

function SettingsTab(props: {
  settings: MonitorSettings;
  busy: string | null;
  workflowState: WorkflowUiState;
  onChange: (settings: MonitorSettings) => void;
  onSave: () => void;
  onRun: () => void;
}) {
  const settings = props.settings;
  const update = (patch: Partial<MonitorSettings>) => props.onChange({ ...settings, ...patch });
  return (
    <section className="panel">
      <div className="section-heading">
        <div>
          <span>GitHub Actions 会使用这些条件</span>
          <h1>查询条件</h1>
        </div>
        <label className="schedule-switch">
          <span>
            <strong>定时查询</strong>
            <em>{settings.schedule_enabled ? '已开启' : '已暂停'}</em>
          </span>
          <input
            type="checkbox"
            checked={settings.schedule_enabled}
            onChange={(event) => update({ schedule_enabled: event.target.checked })}
          />
          <i className="ios-switch" aria-hidden="true" />
        </label>
      </div>
      <div className="form-grid">
        <label className="field full">
          <span>查询时间</span>
          <div className="segmented">
            <button
              className={settings.date_mode === 'relative' ? 'active' : ''}
              onClick={() => update({ date_mode: 'relative' })}
            >
              最近 X 周/月
            </button>
            <button
              className={settings.date_mode === 'range' ? 'active' : ''}
              onClick={() => update({ date_mode: 'range' })}
            >
              指定时间段
            </button>
          </div>
        </label>
        {settings.date_mode === 'relative' ? (
          <>
            <label className="field">
              <span>最近</span>
              <input
                type="number"
                min="1"
                value={settings.lookahead_amount}
                onChange={(event) => update({ lookahead_amount: Number(event.target.value) })}
              />
            </label>
            <label className="field">
              <span>单位</span>
              <select
                value={settings.lookahead_unit}
                onChange={(event) => update({ lookahead_unit: event.target.value as 'weeks' | 'months' })}
              >
                <option value="weeks">周</option>
                <option value="months">月</option>
              </select>
            </label>
          </>
        ) : (
          <>
            <label className="field">
              <span>开始日期</span>
              <input
                type="date"
                value={settings.start_date || ''}
                onChange={(event) => update({ start_date: event.target.value })}
              />
            </label>
            <label className="field">
              <span>结束日期</span>
              <input
                type="date"
                value={settings.end_date || ''}
                onChange={(event) => update({ end_date: event.target.value })}
              />
            </label>
          </>
        )}
        <label className="field">
          <span>露营晚数</span>
          <input
            type="number"
            min="1"
            value={settings.stay_nights}
            onChange={(event) => update({ stay_nights: Number(event.target.value) })}
          />
        </label>
        <label className="field">
          <span>查询间隔</span>
          <div className="input-with-unit">
            <input
              type="number"
              min="1"
              value={settings.query_interval_hours}
              onChange={(event) => update({ query_interval_hours: Number(event.target.value) })}
            />
            <em>小时</em>
          </div>
        </label>
        <label className="toggle-row full">
          <input
            type="checkbox"
            checked={settings.require_weekend_or_holiday}
            onChange={(event) => update({ require_weekend_or_holiday: event.target.checked })}
          />
          <span>周末/节假日：只保留周五、周六、周日和美国联邦假日</span>
        </label>
      </div>
      <div className="action-row">
        <button className="primary-button" onClick={props.onSave} disabled={props.busy === 'save'}>
          <Settings size={16} />
          保存设置
        </button>
        <button className="line-button" onClick={props.onRun} disabled={props.busy === 'workflow'}>
          <Clock3 size={16} />
          立即查询
        </button>
      </div>
      <WorkflowStatusCard state={props.workflowState} />
    </section>
  );
}

function WorkflowStatusCard({ state }: { state: WorkflowUiState }) {
  const Icon = state.tone === 'error' ? AlertCircle : state.tone === 'success' ? CheckCircle2 : Clock3;
  return (
    <div className={`workflow-status ${state.tone}`}>
      <Icon size={18} />
      <div>
        <strong>{state.title}</strong>
        {state.detail && <span>{state.detail}</span>}
      </div>
      {state.url && (
        <a href={state.url} target="_blank" rel="noreferrer">
          查看运行
          <ExternalLink size={14} />
        </a>
      )}
    </div>
  );
}

function ResultsTab(props: { loading: boolean; results: ResultsPayload | null; onRefresh: () => void }) {
  const currentOpenings = props.results?.current_openings || [];
  const newOpenings = props.results?.new_openings || [];
  const openings = currentOpenings.length ? currentOpenings : newOpenings;
  return (
    <section className="panel wide-panel">
      <div className="section-heading inline">
        <div>
          <span>{props.results?.generated_at_display || '暂无运行记录'}</span>
          <h1>查询结果</h1>
        </div>
        <button className="line-button" onClick={props.onRefresh} disabled={props.loading}>
          <CalendarDays size={16} />
          {props.loading ? '刷新中' : '刷新'}
        </button>
      </div>
      <div className="result-table">
        <div className="result-head">
          <span>营地</span>
          <span>Site</span>
          <span>时间</span>
          <span>类型</span>
          <span>链接</span>
        </div>
        {openings.map((opening, index) => (
          <div className="result-row" key={`${opening.campground_name}-${opening.site}-${index}`}>
            <span>
              <strong>{opening.campground_name}</strong>
              <em>{opening.provider || opening.park_name}</em>
            </span>
            <span>{opening.site}</span>
            <span>{opening.stay_dates || opening.date}</span>
            <span>
              {opening.day_type}
              {opening.is_new ? <b>新</b> : null}
            </span>
            <a href={opening.url} target="_blank" rel="noreferrer">
              查看链接
              <ExternalLink size={14} />
            </a>
          </div>
        ))}
        {props.loading && !openings.length && (
          <div className="empty-line roomy">
            <div className="inline-loading">
              <div className="loading-spinner small" />
              正在读取最近一次查询结果
            </div>
          </div>
        )}
        {!props.loading && !openings.length && <div className="empty-line roomy">还没有查询结果</div>}
      </div>
    </section>
  );
}

function LoginScreen({ onSignIn }: { onSignIn: () => void }) {
  return (
    <ShellState
      text="使用 Google 登录"
      detail="只有 allowlist 中的邮箱可以进入系统。"
      action={
        <button className="primary-button" onClick={onSignIn}>
          <ShieldCheck size={16} />
          Google 登录
        </button>
      }
    />
  );
}

function DeniedScreen({ email, onSignOut }: { email: string; onSignOut: () => void }) {
  return (
    <ShellState
      text="没有访问权限"
      detail={`${email} 不在允许访问列表中。`}
      action={
        <button className="line-button" onClick={onSignOut}>
          退出
        </button>
      }
    />
  );
}

function SetupMissing() {
  return (
    <ShellState
      text="Firebase 前端配置缺失"
      detail="请设置 VITE_FIREBASE_API_KEY、VITE_FIREBASE_AUTH_DOMAIN、VITE_FIREBASE_PROJECT_ID 和 VITE_FIREBASE_APP_ID。"
    />
  );
}

function ShellState({
  text,
  detail,
  action,
}: {
  text: string;
  detail?: string;
  action?: React.ReactNode;
}) {
  return (
    <main className="center-state">
      <Trees size={42} />
      <h1>{text}</h1>
      {detail && <p>{detail}</p>}
      {action}
    </main>
  );
}

function sleep(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

createRoot(document.getElementById('root')!).render(<App />);
