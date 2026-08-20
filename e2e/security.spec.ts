import { expect, test } from '@playwright/test';

const baseURL = process.env.NEXA_E2E_URL || 'http://127.0.0.1:4096';
const projectRoot = (process.env.NEXA_E2E_WORKTREE || process.cwd()).replace(/\\/g, '/');

test('console loads only local assets with nonce CSP', async ({ page }) => {
  const externalRequests: string[] = [];
  const pageErrors: string[] = [];
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.protocol !== 'data:' && url.origin !== baseURL) externalRequests.push(request.url());
  });
  page.on('pageerror', error => pageErrors.push(error.message));

  const response = await page.goto(baseURL, { waitUntil: 'domcontentloaded' });
  expect(response?.status()).toBe(200);
  await expect(page).toHaveTitle('Nexa');
  await expect(page.getByText('Nexa', { exact: true }).first()).toBeVisible();
  const csp = response?.headers()['content-security-policy'] || '';
  expect(csp).toContain("script-src 'self' 'nonce-");
  expect(csp).not.toContain("script-src 'self' 'unsafe-inline'");
  expect(externalRequests).toEqual([]);
  expect(pageErrors).toEqual([]);
  const scriptSources = await page.locator('script[src]').evaluateAll(nodes =>
    nodes.map(node => (node as HTMLScriptElement).src),
  );
  expect(scriptSources.length).toBe(5);
  expect(scriptSources.every(source => source.startsWith(`${baseURL}/static/vendor/`))).toBe(true);
  expect((await page.request.get(`${baseURL}/static/index.html`)).status()).toBe(404);
  expect((await page.request.get(`${baseURL}/static/index-old.html`)).status()).toBe(404);
});

test('markdown renderer preserves formatting and removes executable markup', async ({ page }) => {
  await page.goto(baseURL, { waitUntil: 'domcontentloaded' });
  await expect(page.getByText('Nexa', { exact: true }).first()).toBeVisible();
  const result = await page.evaluate(() => {
    const renderMd = (window as unknown as { renderMd: (text: string) => string }).renderMd;
    (window as unknown as { __p001Xss?: number }).__p001Xss = 0;
    const malicious = [
      '# Safe title',
      '<img src=x onerror="window.__p001Xss=1">',
      '<svg onload="window.__p001Xss=2"><script>window.__p001Xss=3</script></svg>',
      '[bad](javascript:window.__p001Xss=4)',
      '<iframe srcdoc="<script>window.__p001Xss=5</script>"></iframe>',
      '**bold** and `code` and [safe](https://example.com/path)',
    ].join('\n\n');
    const html = renderMd(malicious);
    const container = document.createElement('div');
    container.innerHTML = html;
    document.body.appendChild(container);
    const links = [...container.querySelectorAll('a')].map(link => ({
      href: link.getAttribute('href'),
      rel: link.getAttribute('rel'),
      target: link.getAttribute('target'),
    }));
    return {
      html,
      xss: (window as unknown as { __p001Xss?: number }).__p001Xss,
      scripts: container.querySelectorAll('script').length,
      images: container.querySelectorAll('img').length,
      frames: container.querySelectorAll('iframe').length,
      svgs: container.querySelectorAll('svg').length,
      eventAttributes: container.querySelectorAll('[onerror], [onload], [onclick]').length,
      heading: container.querySelector('h1')?.textContent,
      bold: container.querySelector('strong')?.textContent,
      code: container.querySelector('code')?.textContent,
      links,
    };
  });

  expect(result.xss).toBe(0);
  expect(result.scripts).toBe(0);
  expect(result.images).toBe(0);
  expect(result.frames).toBe(0);
  expect(result.svgs).toBe(0);
  expect(result.eventAttributes).toBe(0);
  expect(result.html.toLowerCase()).not.toContain('javascript:');
  expect(result.heading).toBe('Safe title');
  expect(result.bold).toBe('bold');
  expect(result.code).toBe('code');
  expect(result.links).toContainEqual({
    href: 'https://example.com/path',
    rel: 'noopener noreferrer',
    target: '_blank',
  });
});

test('real console input path stores and renders hostile user markdown safely', async ({ page }) => {
  const title = `P0-01-console-input-${Date.now()}`;
  await page.goto(baseURL, { waitUntil: 'domcontentloaded' });
  const normalizedProjectRoot = projectRoot.toLowerCase();
  const projectsResponse = await page.request.get(`${baseURL}/projects`);
  const existingProjects = (await projectsResponse.json()).projects as Array<{
    id: string;
    name: string;
    root_path: string;
  }>;
  let project = existingProjects.find(
    item => item.root_path.replace(/\\/g, '/').toLowerCase() === normalizedProjectRoot,
  );
  if (!project) {
    const projectName = `P0-01-project-${Date.now()}`;
    await page.getByRole('button', { name: '+ New Project' }).click();
    const projectForm = page.locator('.new-session-form');
    await projectForm.locator('input').nth(0).fill(projectRoot);
    await projectForm.locator('input').nth(1).fill(projectName);
    await page.getByRole('button', { name: 'Add project' }).click();
    const refreshedProjects = (await (await page.request.get(`${baseURL}/projects`)).json()).projects as Array<{
      id: string;
      name: string;
      root_path: string;
    }>;
    project = refreshedProjects.find(item => item.name === projectName);
  }
  expect(project).toBeTruthy();
  await page.locator('.project-row').filter({ hasText: project!.name }).click();
  await page.getByRole('button', { name: '+ New thread' }).click();
  await page.locator('.new-session-form input[placeholder="New thread"]').fill(title);
  await page.getByRole('button', { name: 'Create thread' }).click();
  await expect(page.locator('.session-title-text', { hasText: title })).toBeVisible();

  const autorun = page.locator('.chat-header input[type="checkbox"]');
  await expect(autorun).toBeChecked();
  await autorun.uncheck();
  const payload = 'P0-01 INPUT SAFE <img src=x onerror="window.__consoleXss=1"> **safe-bold** [bad](javascript:window.__consoleXss=2)';
  await page.locator('.composer-textarea').fill(payload);
  await page.locator('.composer-textarea').press('Enter');

  const userMessage = page.locator('.message.user').last();
  await expect(userMessage).toContainText('P0-01 INPUT SAFE');
  await expect(userMessage.locator('strong')).toHaveText('safe-bold');
  await expect(userMessage.locator('img, script, svg, iframe')).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __consoleXss?: number }).__consoleXss || 0)).toBe(0);

  const sessionsResponse = await page.request.get(`${baseURL}/sessions`);
  const sessions = (await sessionsResponse.json()).sessions as Array<{ id: string; title: string }>;
  const created = sessions.find(session => session.title === title);
  expect(created).toBeTruthy();
  if (created) {
    const messages = await (await page.request.get(`${baseURL}/sessions/${created.id}/messages`)).json();
    expect(JSON.stringify(messages)).toContain('P0-01 INPUT SAFE');
    expect((await page.request.delete(`${baseURL}/sessions/${created.id}`)).status()).toBe(200);
  }
});

test('config endpoints never return secrets and reject cross-origin mutation', async ({ request }) => {
  const config = await request.get(`${baseURL}/config`);
  expect(config.status()).toBe(200);
  const publicConfig = await config.json();
  expect(publicConfig.openai.api_key).toHaveProperty('configured');
  expect(typeof publicConfig.openai.api_key.configured).toBe('boolean');
  expect(typeof publicConfig.openai.api_key).not.toBe('string');
  expect(config.headers()['cache-control']).toBe('no-store');

  for (const scope of ['project', 'global']) {
    const raw = await request.get(`${baseURL}/config/raw`, { params: { scope } });
    expect(raw.status()).toBe(200);
    const body = await raw.json();
    expect(body.content).not.toMatch(/sk-(?:proj|or|live)-[A-Za-z0-9_-]+/);
    for (const field of body.sensitive_fields_set || []) {
      expect(body.content).toContain('<redacted>');
      expect(typeof field).toBe('string');
    }
  }

  const title = `P0-01-cross-origin-${Date.now()}`;
  const blocked = await request.post(`${baseURL}/sessions`, {
    headers: { Origin: 'https://evil.example', 'Sec-Fetch-Site': 'cross-site' },
    data: { worktree: projectRoot, title },
  });
  expect(blocked.status()).toBe(403);
  expect((await blocked.json()).error.code).toBe('cross_site_request_denied');
  const sessions = await (await request.get(`${baseURL}/sessions`)).json();
  expect(sessions.sessions.some((session: { title: string }) => session.title === title)).toBe(false);

  const untrustedHost = await request.get(`${baseURL}/`, { headers: { Host: 'evil.example' } });
  expect(untrustedHost.status()).toBe(400);
  expect((await untrustedHost.json()).error.code).toBe('untrusted_host');
});
