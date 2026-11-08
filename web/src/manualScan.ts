export type BrowserScanPlan = {
  id: string;
  requests: {
    key: string;
    provider: string;
    campground: { campground_name: string };
    url: string;
    method: 'GET' | 'POST';
    body?: Record<string, unknown>;
  }[];
};

export async function collectBrowserAvailability(
  plan: BrowserScanPlan,
  onProgress: (message: string) => void,
): Promise<{ key: string; payload: Record<string, unknown> }[]> {
  const results = [];
  for (let index = 0; index < plan.requests.length; index += 1) {
    const request = plan.requests[index];
    const url = new URL(request.url);
    const expectedHost = request.provider === 'Recreation.gov'
      ? 'www.recreation.gov'
      : 'california-rdr.prod.cali.rd12.recreation-management.tylerapp.com';
    if (url.protocol !== 'https:' || url.hostname !== expectedHost) {
      throw new Error('营地查询地址无效。');
    }
    onProgress(`正在查询 ${request.campground.campground_name}（${index + 1}/${plan.requests.length}），请保持页面打开。`);
    // Provider data is public. Never forward the user's Firebase token or cookies.
    const response = await fetch(request.url, {
      method: request.method,
      headers: request.body ? { 'Content-Type': 'application/json' } : undefined,
      body: request.body ? JSON.stringify(request.body) : undefined,
      credentials: 'omit',
      signal: AbortSignal.timeout(30000),
    });
    if (!response.ok) {
      throw new Error(`${request.provider} 查询失败（HTTP ${response.status}）；原有结果已保留。`);
    }
    results.push({ key: request.key, payload: await response.json() });
    if (index + 1 < plan.requests.length) {
      await new Promise((resolve) => setTimeout(resolve, 1100));
    }
  }
  return results;
}
