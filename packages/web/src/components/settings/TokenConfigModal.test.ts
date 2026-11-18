import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import TokenConfigModal from './TokenConfigModal.vue'
import { useProjectStore } from '@/stores/project'
import type { Project, ProjectSecret } from '@/types'

const apiMock = vi.hoisted(() => ({ apiFetch: vi.fn() }))
const secretsMock = vi.hoisted(() => ({
  listProjectSecrets: vi.fn(),
  createProjectSecret: vi.fn(),
  updateProjectSecret: vi.fn(),
  deleteProjectSecret: vi.fn(),
}))
const agentsMock = vi.hoisted(() => ({
  getAgents: vi.fn(),
  getModelConfigs: vi.fn(),
  syncAgents: vi.fn(),
}))

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, apiFetch: apiMock.apiFetch }
})
vi.mock('@/api/projectSecrets', () => secretsMock)
vi.mock('@/api/agents', () => ({ agentsApi: agentsMock }))
vi.mock('@/api/modelConfigs', () => ({
  modelConfigsApi: { testConnection: vi.fn() },
}))

function makeProject(id: string): Project {
  return {
    project_id: id,
    slug: id,
    display_name: id,
    parent_id: null,
    fs_path: null,
    can_delete: true,
    category: 'default',
    created_by: 'u-1',
    visibility: 'private',
    is_owner: true,
    can_manage: true,
  }
}

function makeSecret(serviceKey: string): ProjectSecret {
  return {
    secret_id: `s-${serviceKey}`,
    service_key: serviceKey,
    environment: null,
    secret_name: serviceKey.toUpperCase().replace(':', '_'),
    display_name: null,
    key_hint: 'you@example.com',
    created_by: 'u-1',
    updated_at: null,
  }
}

function stubApi() {
  apiMock.apiFetch.mockImplementation((url: string) => {
    if (url === '/api/integrations/defaults') return Promise.resolve({})
    if (url === '/api/tokens') return Promise.resolve([])
    if (url.endsWith('/mcp-servers')) return Promise.resolve([])
    return Promise.resolve({})
  })
  agentsMock.getModelConfigs.mockResolvedValue([])
}

async function mountOnIntegrationsTab() {
  const wrapper = mount(TokenConfigModal, {
    global: { stubs: { teleport: true } },
  })
  await flushPromises()
  await nextTick()
  await wrapper.findAll('.token-tab')[1].trigger('click')
  await nextTick()
  return wrapper
}

describe('TokenConfigModal integration scope detection', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    stubApi()
    useProjectStore().currentProject = makeProject('p1')
  })

  it('an extra-field-only project secret marks the service configured in project scope', async () => {
    // An agent may save only "jira:email" into project_secrets, with no
    // "jira" row. The service is still configured project-side: hiding it
    // would offer to add it again and write the typed token to user_tokens.
    secretsMock.listProjectSecrets.mockResolvedValue([makeSecret('jira:email')])
    const wrapper = await mountOnIntegrationsTab()

    const jiraRow = wrapper
      .findAll('.token-service-row')
      .find((row) => row.text().includes('Jira'))
    expect(jiraRow).toBeDefined()
    expect(jiraRow!.find('.scope-badge.project-scope').exists()).toBe(true)

    // The "add integration" picker must not offer Jira a second time.
    await wrapper.find('.token-add-section .add-model-btn').trigger('click')
    const offered = wrapper.findAll('.token-add-form option').map((o) => o.text())
    expect(offered).not.toContain('Jira')
    expect(offered).toContain('GitHub / Git')
  })

  it('saves a typed main token when only an extra-field project secret exists', async () => {
    // The service is already project-configured through "jira:email" alone.
    // Gating the project branch on the main secret ran neither branch and
    // still reported "saved": the typed token never reached the server.
    secretsMock.listProjectSecrets.mockResolvedValue([makeSecret('jira:email')])
    secretsMock.createProjectSecret.mockResolvedValue({
      secret_id: 's-jira',
      service_key: 'jira',
      key_hint: '...ken',
    })
    const wrapper = await mountOnIntegrationsTab()

    const jiraRow = wrapper
      .findAll('.token-service-row')
      .find((row) => row.text().includes('Jira'))!
    expect(jiraRow.find('.scope-badge.project-scope').exists()).toBe(true)

    // Project scope hides base_url: main key + jira:email.
    const inputs = jiraRow.findAll('.token-edit-block input')
    expect(inputs).toHaveLength(2)
    await inputs[0].setValue('new-jira-token')
    await jiraRow.find('.token-row-actions button').trigger('click')
    await flushPromises()

    expect(secretsMock.createProjectSecret).toHaveBeenCalledWith('p1', {
      service_key: 'jira',
      value: 'new-jira-token',
    })
    expect(secretsMock.updateProjectSecret).not.toHaveBeenCalled()
    // The row is re-rendered after the save, so query it again rather than
    // asserting against the detached pre-click node.
    const savedRow = wrapper
      .findAll('.token-service-row')
      .find((row) => row.text().includes('Jira'))!
    expect(savedRow.text()).toContain('✓')
    expect(savedRow.text()).toContain('...ken')
  })

  it('cancelling an add leaves no extra-field values behind', async () => {
    secretsMock.listProjectSecrets.mockResolvedValue([])
    const wrapper = await mountOnIntegrationsTab()

    await wrapper.find('.token-add-section .add-model-btn').trigger('click')
    const select = wrapper.find('.token-add-form select')
    await select.setValue('jira')
    const extra = wrapper.find('.token-add-form .token-url-input')
    await extra.setValue('someone@example.com')
    // Cancel is the third action (add / test / cancel).
    await wrapper.findAll('.token-add-form .token-row-actions button')[2].trigger('click')
    await nextTick()

    // Re-open and pick the same service: the old email must not pre-fill.
    await wrapper.find('.token-add-section .add-model-btn').trigger('click')
    await wrapper.find('.token-add-form select').setValue('jira')
    expect(
      (wrapper.find('.token-add-form .token-url-input').element as HTMLInputElement).value,
    ).toBe('')
  })

  it('no secret at all leaves the service in the add picker', async () => {
    secretsMock.listProjectSecrets.mockResolvedValue([])
    const wrapper = await mountOnIntegrationsTab()

    const rows = wrapper.findAll('.token-service-label').map((n) => n.text())
    expect(rows).not.toContain('Jira')

    await wrapper.find('.token-add-section .add-model-btn').trigger('click')
    const offered = wrapper.findAll('.token-add-form option').map((o) => o.text())
    expect(offered).toContain('Jira')
  })

  it('keeps the typed token after a project-scope failure so the retry writes it', async () => {
    // Scope 'both': the user-token branch used to clear the input before the
    // project_secrets write ran. When that write failed, the retry saw an
    // empty input, ran neither branch, and still reported "saved" — the
    // project copy was never written.
    apiMock.apiFetch.mockImplementation((url: string, options?: RequestInit) => {
      if (url === '/api/integrations/defaults') return Promise.resolve({})
      if (url === '/api/tokens') {
        return Promise.resolve([{ service_key: 'git', key_hint: '...old', base_url: null }])
      }
      if (url === '/api/tokens/git' && options?.method === 'PUT') {
        return Promise.resolve({ key_hint: '...new' })
      }
      if (url.endsWith('/mcp-servers')) return Promise.resolve([])
      return Promise.resolve({})
    })
    secretsMock.listProjectSecrets.mockResolvedValue([makeSecret('git')])
    secretsMock.updateProjectSecret
      .mockRejectedValueOnce(new Error('project vault down'))
      .mockResolvedValue({ secret_id: 's-git', key_hint: '...new' })

    const wrapper = await mountOnIntegrationsTab()
    const row = wrapper
      .findAll('.token-service-row')
      .find((r) => r.text().includes('GitHub / Git'))!
    // 'both' scope shows base_url plus the main token input.
    const inputs = row.findAll('.token-edit-block input')
    await inputs[1].setValue('new-token')
    await row.find('.token-row-actions button').trigger('click')
    await flushPromises()
    expect(secretsMock.updateProjectSecret).toHaveBeenCalledTimes(1)

    // Retry without retyping: the input must still hold the value.
    const retryRow = wrapper
      .findAll('.token-service-row')
      .find((r) => r.text().includes('GitHub / Git'))!
    const retryInputs = retryRow.findAll('.token-edit-block input')
    expect((retryInputs[1].element as HTMLInputElement).value).toBe('new-token')
    await retryRow.find('.token-row-actions button').trigger('click')
    await flushPromises()

    expect(secretsMock.updateProjectSecret).toHaveBeenCalledTimes(2)
    expect(secretsMock.updateProjectSecret).toHaveBeenLastCalledWith('p1', 's-git', {
      value: 'new-token',
    })
    // Re-query: the row re-rendered after the save, so the pre-click wrapper
    // may still show the earlier failure message.
    const savedRow = wrapper
      .findAll('.token-service-row')
      .find((r) => r.text().includes('GitHub / Git'))!
    expect(savedRow.text()).toContain('✓')
  })
})

describe('TokenConfigModal add-model form', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    stubApi()
    useProjectStore().currentProject = makeProject('p1')
  })

  it('cancel wipes the abandoned add-model values', async () => {
    // Cancel used to only hide the form: provider, model id and the API key
    // stayed behind and silently seeded the next add attempt.
    const wrapper = mount(TokenConfigModal, {
      global: { stubs: { teleport: true } },
    })
    await flushPromises()

    await wrapper.find('.add-model-btn').trigger('click')
    const form = wrapper.find('.token-add-form')
    await form.find('select').setValue('openai')
    const inputs = form.findAll('input')
    await inputs[0].setValue('gpt-5')
    await inputs[1].setValue('sk-secret')
    // add / test / cancel
    await form.findAll('.token-row-actions button')[2].trigger('click')
    await nextTick()

    await wrapper.find('.add-model-btn').trigger('click')
    const reopened = wrapper.find('.token-add-form')
    const reopenedInputs = reopened.findAll('input')
    expect((reopenedInputs[0].element as HTMLInputElement).value).toBe('')
    expect((reopenedInputs[1].element as HTMLInputElement).value).toBe('')
    expect((reopened.find('select').element as HTMLSelectElement).value).toBe('')
  })
})

describe('TokenConfigModal — context-overflow targeting', () => {
  function makeModelConfig(configId: string, modelId: string) {
    return {
      config_id: configId,
      provider: 'openai',
      model_id: modelId,
      key_hint: null,
      base_url: null,
      url_mode: 'base_url' as const,
      complexity_tier: null,
      context_window: null,
      default_context_window: 128_000,
      thinking_enabled: null,
      reasoning_effort: null,
      is_system: false,
    }
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    stubApi()
    useProjectStore().currentProject = makeProject('p1')
    // The row element id is 'cfg-' + config_id — plain ids keep the
    // expectations below readable.
    agentsMock.getModelConfigs.mockResolvedValue([
      makeModelConfig('row-a', 'gpt-5'),
      makeModelConfig('row-b', 'gpt-5-mini'),
    ])
    // happy-dom has no layout engine — the call must exist to be asserted.
    Element.prototype.scrollIntoView = vi.fn()
  })

  it('scrolls to and focuses the targeted row\'s context-window input', async () => {
    // Real Teleport (not the stub): the stub re-creates its subtree on every
    // re-render, which disconnects the focused input before assertions run.
    // The targeting uses document.getElementById, so the tree must be in
    // document.body — the real Teleport puts it there.
    const wrapper = mount(TokenConfigModal, {
      props: { targetConfigId: 'row-b' },
    })
    await flushPromises()
    // The row appears after fetchModelConfigs resolves; the watcher retries
    // until it lands in the DOM.
    await nextTick()
    await nextTick()

    const row = document.getElementById('cfg-row-b')
    expect(row).not.toBeNull()
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled()
    const input = row!.querySelector('input[data-field="context_window"]') as HTMLInputElement
    expect(document.activeElement).toBe(input)
    expect(row!.classList.contains('token-service-row--highlight')).toBe(true)
    // The other row is untouched.
    expect(document.getElementById('cfg-row-a')!.classList.contains('token-service-row--highlight')).toBe(false)
    wrapper.unmount()
  })

  it('an unknown config id degrades to a plain open', async () => {
    const wrapper = mount(TokenConfigModal, {
      props: { targetConfigId: 'row-missing' },
    })
    await flushPromises()
    await nextTick()
    await nextTick()

    expect(document.getElementById('cfg-row-missing')).toBeNull()
    expect(Element.prototype.scrollIntoView).not.toHaveBeenCalled()
    // Existing rows still render.
    expect(document.getElementById('cfg-row-a')).not.toBeNull()
    wrapper.unmount()
  })

  it('no target prop renders plainly (settings page without query)', async () => {
    const wrapper = mount(TokenConfigModal)
    await flushPromises()
    await nextTick()

    expect(Element.prototype.scrollIntoView).not.toHaveBeenCalled()
    expect(document.querySelector('.token-service-row--highlight')).toBeNull()
    wrapper.unmount()
  })
})

describe('TokenConfigModal — email servers', () => {
  const EMAIL = {
    email_server_id: 'e1',
    slug: 'recruiting-inbox',
    display_name: '招聘收件箱',
    imap_host: 'imap.example.com',
    imap_port: 993,
    imap_security: 'ssl',
    smtp_host: 'smtp.example.com',
    smtp_port: 587,
    smtp_security: 'starttls',
    username: 'hr@example.com',
    key_hint: '...pass',
    allow_private_network: false,
    enabled: true,
  }

  function stubEmail(servers: typeof EMAIL[] = []) {
    apiMock.apiFetch.mockImplementation((url: string, options?: RequestInit) => {
      if (url === '/api/integrations/defaults') return Promise.resolve({})
      if (url === '/api/tokens') return Promise.resolve([])
      if (url.endsWith('/mcp-servers')) return Promise.resolve([])
      if (url === '/api/email/servers') {
        if (options?.method === 'POST') return Promise.resolve({})
        return Promise.resolve(servers)
      }
      return Promise.resolve({})
    })
  }

  // The Teleport stub recreates its subtree on re-render, so a captured
  // section node goes stale after any click that toggles the form. Always
  // re-query.
  function emailSectionOf(wrapper: Awaited<ReturnType<typeof mountOnIntegrationsTab>>) {
    return wrapper.findAll('.mcp-section').find((s) => s.text().includes('邮件服务器'))!
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    stubEmail()
    useProjectStore().currentProject = makeProject('p1')
  })

  it('shows the empty state until a server is added', async () => {
    const wrapper = await mountOnIntegrationsTab()
    expect(emailSectionOf(wrapper).text()).toContain('未配置邮件服务器')
  })

  it('creates a server and sends the typed password', async () => {
    const wrapper = await mountOnIntegrationsTab()
    await emailSectionOf(wrapper).find('.add-model-btn').trigger('click')
    await nextTick()

    const inputs = emailSectionOf(wrapper).findAll('.mcp-form-grid input')
    // display_name, slug, imap_host, imap_port, smtp_host, smtp_port, username, password
    await inputs[0].setValue('招聘收件箱')
    await inputs[1].setValue('recruiting-inbox')
    await inputs[2].setValue('imap.example.com')
    await inputs[4].setValue('smtp.example.com')
    await inputs[6].setValue('hr@example.com')
    await inputs[7].setValue('sup3r-s3cret')
    await emailSectionOf(wrapper).find('.token-row-actions button').trigger('click')
    await flushPromises()

    const postCall = apiMock.apiFetch.mock.calls.find(
      ([url, opts]) => url === '/api/email/servers' && opts?.method === 'POST',
    )
    expect(postCall).toBeDefined()
    const body = JSON.parse(postCall![1].body as string)
    expect(body.slug).toBe('recruiting-inbox')
    expect(body.password).toBe('sup3r-s3cret')
  })

  it('edit with an empty password keeps the stored one (password: null)', async () => {
    stubEmail([EMAIL])
    const wrapper = await mountOnIntegrationsTab()
    // Listed row shows the badge but never a plaintext password.
    expect(emailSectionOf(wrapper).text()).toContain('...pass')
    expect(emailSectionOf(wrapper).text()).not.toContain('sup3r')

    // Edit is the first per-item action.
    await emailSectionOf(wrapper).findAll('.mcp-item .btn-sm')[0].trigger('click')
    await nextTick()

    const passwordInput = emailSectionOf(wrapper).findAll('.mcp-form-grid input')
      .find((i) => (i.element as HTMLInputElement).type === 'password')!
    expect((passwordInput.element as HTMLInputElement).placeholder).toContain('留空')
    await passwordInput.setValue('')
    await emailSectionOf(wrapper).find('.token-row-actions button').trigger('click')
    await flushPromises()

    const putCall = apiMock.apiFetch.mock.calls.find(
      ([url, opts]) => url === '/api/email/servers/e1' && opts?.method === 'PUT',
    )
    expect(putCall).toBeDefined()
    expect(JSON.parse(putCall![1].body as string).password).toBeNull()
  })

  it('lists multiple servers with IMAP/SMTP host:port and username', async () => {
    const second = { ...EMAIL, email_server_id: 'e2', slug: 'hr-personal', display_name: 'HR 个人邮箱', username: 'someone@example.com' }
    stubEmail([EMAIL, second])
    const wrapper = await mountOnIntegrationsTab()
    const emailSection = emailSectionOf(wrapper)
    expect(emailSection.findAll('.mcp-item')).toHaveLength(2)
    expect(emailSection.text()).toContain('IMAP imap.example.com:993')
    expect(emailSection.text()).toContain('SMTP smtp.example.com:587')
    expect(emailSection.text()).toContain('someone@example.com')
  })
})

describe('TokenConfigModal — MCP secret configuration', () => {
  const MCP_USER = {
    server_id: 'm1',
    slug: 'github-copilot',
    name: 'GitHub Copilot',
    url: 'https://mcp.example.com/mcp',
    transport: 'auto',
    enabled: true,
    auth_type: 'bearer',
    secret_ref: 'mcp:github-copilot',
    auth_header_name: null,
    has_secret: false,
    secret_scope: 'user' as const,
    tool_allowlist: [],
    tool_denylist: [],
    scope: 'global' as const,
  }
  const MCP_PROJECT = {
    ...MCP_USER,
    server_id: 'm2',
    slug: 'proj-srv',
    name: 'Project Server',
    secret_ref: 'mcp:proj-srv',
    secret_scope: 'project' as const,
    scope: 'project' as const,
  }

  function stubMcp(servers: Array<Omit<typeof MCP_USER, 'secret_scope' | 'scope'> & {
    secret_scope: 'user' | 'project'
    scope: 'global' | 'project'
  }>) {
    apiMock.apiFetch.mockImplementation((url: string, options?: RequestInit) => {
      if (url === '/api/integrations/defaults') return Promise.resolve({})
      if (url === '/api/tokens') return Promise.resolve([])
      if (url.endsWith('/mcp-servers')) return Promise.resolve(servers)
      if (url.endsWith('/test') && options?.method === 'POST') {
        return Promise.resolve({ ok: true, tools: 3 })
      }
      if (options?.method === 'PUT' || options?.method === 'POST') return Promise.resolve({})
      return Promise.resolve({})
    })
    secretsMock.listProjectSecrets.mockResolvedValue([])
  }

  function mcpSectionOf(wrapper: Awaited<ReturnType<typeof mountOnIntegrationsTab>>) {
    return wrapper.findAll('.mcp-section').find((s) => s.text().includes('MCP 服务器'))!
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    stubMcp([MCP_USER, MCP_PROJECT])
    useProjectStore().currentProject = makeProject('p1')
  })

  it('user-scope row saves the secret value to the user vault', async () => {
    const wrapper = await mountOnIntegrationsTab()
    const section = mcpSectionOf(wrapper)
    const row = section.findAll('.mcp-item').find((r) => r.text().includes('GitHub Copilot'))!

    await row.findAll('.btn-sm').find((b) => b.text() === '配置密钥')!.trigger('click')
    await nextTick()
    const input = wrapper.find('.mcp-secret-input')
    expect(input.exists()).toBe(true)
    await input.setValue('tok-123')
    await wrapper.findAll('.mcp-item .btn-sm').find((b) => b.text() === '保存')!.trigger('click')
    await flushPromises()

    const putCall = apiMock.apiFetch.mock.calls.find(
      ([url, opts]) => url === `/api/tokens/${encodeURIComponent('mcp:github-copilot')}` && opts?.method === 'PUT',
    )
    expect(putCall).toBeDefined()
    expect(JSON.parse(putCall![1].body as string)).toEqual({ value: 'tok-123' })
    // The list reloads so the badge refreshes.
    expect(apiMock.apiFetch).toHaveBeenCalledWith('/api/projects/p1/mcp-servers')
  })

  it('project-scope row saves the secret value to project secrets', async () => {
    const wrapper = await mountOnIntegrationsTab()
    const section = mcpSectionOf(wrapper)
    const row = section.findAll('.mcp-item').find((r) => r.text().includes('Project Server'))!

    await row.findAll('.btn-sm').find((b) => b.text() === '配置密钥')!.trigger('click')
    await nextTick()
    await wrapper.find('.mcp-secret-input').setValue('proj-tok')
    await wrapper.findAll('.mcp-item .btn-sm').find((b) => b.text() === '保存')!.trigger('click')
    await flushPromises()

    expect(secretsMock.createProjectSecret).toHaveBeenCalledWith('p1', {
      service_key: 'mcp:proj-srv',
      value: 'proj-tok',
    })
  })

  it('test button sends the current project_id in the body', async () => {
    const wrapper = await mountOnIntegrationsTab()
    const section = mcpSectionOf(wrapper)
    const row = section.findAll('.mcp-item').find((r) => r.text().includes('GitHub Copilot'))!

    // 测试 is the second per-item action for global rows (编辑 / 测试 / 删除).
    await row.findAll('.btn-sm').find((b) => b.text() === '测试')!.trigger('click')
    await flushPromises()

    const testCall = apiMock.apiFetch.mock.calls.find(
      ([url, opts]) => url === '/api/mcp/servers/m1/test' && opts?.method === 'POST',
    )
    expect(testCall).toBeDefined()
    expect(JSON.parse(testCall![1].body as string)).toEqual({ project_id: 'p1' })
  })

  it('SECRET_MISSING renders the configure-secret hint, not the generic failure', async () => {
    apiMock.apiFetch.mockImplementation((url: string, options?: RequestInit) => {
      if (url === '/api/integrations/defaults') return Promise.resolve({})
      if (url === '/api/tokens') return Promise.resolve([])
      if (url.endsWith('/mcp-servers')) return Promise.resolve([MCP_USER])
      if (url.endsWith('/test') && options?.method === 'POST') {
        return Promise.resolve({
          ok: false,
          tools: 0,
          code: 'SECRET_MISSING',
          error: "Secret not configured for key 'mcp:github-copilot'.",
        })
      }
      return Promise.resolve({})
    })
    const wrapper = await mountOnIntegrationsTab()
    const section = mcpSectionOf(wrapper)
    const row = section.findAll('.mcp-item').find((r) => r.text().includes('GitHub Copilot'))!
    await row.findAll('.btn-sm').find((b) => b.text() === '测试')!.trigger('click')
    await flushPromises()

    const msg = wrapper.find('.mcp-inline-msg')
    expect(msg.exists()).toBe(true)
    expect(msg.text()).toContain('密钥未配置：mcp:github-copilot')
    expect(msg.text()).not.toContain('连接失败或未发现工具')
  })

  it('saving a new global server derives slug and secret key from the name', async () => {
    const wrapper = await mountOnIntegrationsTab()
    await mcpSectionOf(wrapper).find('.add-model-btn').trigger('click')
    await nextTick()
    // The Teleport stub rebuilds its subtree on re-render — re-query.
    let section = mcpSectionOf(wrapper)

    const inputs = section.findAll('.mcp-form-grid input')
    // name, url (slug and secret_ref are derived — no inputs)
    await inputs[0].setValue('New Server')
    await inputs[1].setValue('https://mcp.example.com/new')
    // selects: transport first, auth_type second
    await section.findAll('.mcp-form-grid select')[1].setValue('bearer')
    await nextTick()
    // The derived preview shows both values before saving.
    section = mcpSectionOf(wrapper)
    expect(section.find('.mcp-derived-hint').text()).toContain('new-server')
    expect(section.find('.mcp-derived-hint').text()).toContain('mcp:new-server')

    await mcpSectionOf(wrapper).find('.token-row-actions button').trigger('click')
    await flushPromises()

    const postCall = apiMock.apiFetch.mock.calls.find(
      ([url, opts]) => url === '/api/mcp/servers' && opts?.method === 'POST',
    )
    expect(postCall).toBeDefined()
    const body = JSON.parse(postCall![1].body as string)
    expect(body.slug).toBe('new-server')
    expect(body.secret_scope).toBe('user')
    expect(body.secret_ref).toBe('mcp:new-server')
  })

  it('editing keeps the stored slug and secret key even when the name changes', async () => {
    const wrapper = await mountOnIntegrationsTab()
    const section = mcpSectionOf(wrapper)
    const row = section.findAll('.mcp-item').find((r) => r.text().includes('GitHub Copilot'))!
    await row.findAll('.btn-sm').find((b) => b.text() === '编辑')!.trigger('click')
    await nextTick()

    const form = mcpSectionOf(wrapper)
    await form.findAll('.mcp-form-grid input')[0].setValue('Renamed Server')
    await mcpSectionOf(wrapper).find('.token-row-actions button').trigger('click')
    await flushPromises()

    const putCall = apiMock.apiFetch.mock.calls.find(
      ([url, opts]) => url === '/api/mcp/servers/m1' && opts?.method === 'PUT',
    )
    expect(putCall).toBeDefined()
    const body = JSON.parse(putCall![1].body as string)
    expect(body.slug).toBe('github-copilot')
    expect(body.secret_ref).toBe('mcp:github-copilot')
    expect(body.name).toBe('Renamed Server')
  })
})
