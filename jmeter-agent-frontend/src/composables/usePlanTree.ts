import type { Ref } from 'vue'
import type { AssertionRule } from '@/components/editor/AssertionsTab.vue'

// The form view edits a plan's tree through these: the tree is read into endpoints and settings, and saving
// patches only what changed back into a copy of the loaded tree, so everything the form cannot show survives.

export interface EndpointData {
  id: string
  _rawTimeout?: unknown
  name: string
  description: string
  method: string
  path: string
  domain: string
  port: string
  protocol: string
  headers: Array<{ name: string; value: string }>
  cookies: Array<{ name: string; value: string; domain: string; path: string }>
  queryParams: Array<{ name: string; value: string; description: string }>
  bodyType: 'none' | 'json' | 'form-data' | 'x-www-form-urlencoded' | 'raw'
  bodyContent: string
  auth: { type: string; username: string; password: string }
  preScript: { language: string; script: string }
  postScript: { language: string; script: string }
  assertions: AssertionRule[]
  extractors: any[]
  settings: {
    timeout: number
    followRedirects: boolean
    autoRedirects: boolean
    keepAlive: boolean
    encoding: string
  }
}

export interface ThreadGroupConfig {
  numThreads: number
  rampTime: number
  loops: number
  duration: number
  delay: number
  scheduler: boolean
  onSampleError: string
}

export interface CsvDatasetConfig {
  filePath: string
  variableNames: string
  delimiter: string
  encoding: string
  ignoreFirstLine: boolean
  recycle: boolean
}

export interface PlanTreeState {
  planId: Ref<string>
  treeData: Ref<any>
  threadGroupNode: Ref<any>
  endpoints: Ref<Map<string, any>>
  threadGroupConfig: ThreadGroupConfig
  csvDatasetConfig: CsvDatasetConfig
  t: (key: string, args?: any) => string
}

export function usePlanTree(state: PlanTreeState) {
  const { planId, treeData, threadGroupNode, endpoints, threadGroupConfig, csvDatasetConfig, t } = state

  function isThreadGroup(node: any) {
    return ['ThreadGroup', 'SetupThreadGroup', 'PostThreadGroup'].includes(node.testclass)
  }

  function isCsvDataset(node: any) {
    return ['CSVDataSet', 'CSVDataSetConfig'].includes(node.testclass)
  }

  function readThreadGroupConfig(node: any) {
    const p = node?.properties || {}
    const number = (key: string, fallback: number) => {
      const value = Number(p[key] ?? fallback)
      return Number.isFinite(value) ? value : fallback
    }
    return {
      numThreads: number('ThreadGroup.num_threads', 1), rampTime: number('ThreadGroup.ramp_time', 1),
      loops: number('loops', 1), duration: number('ThreadGroup.duration', 0), delay: number('ThreadGroup.delay', 0),
      scheduler: p['ThreadGroup.scheduler'] === true || p['ThreadGroup.scheduler'] === 'true',
      onSampleError: p['ThreadGroup.on_sample_error'] ?? 'continue',
    }
  }

  function threadGroupNumberValue(property: string, fallback: number): any {
    if (property === 'ThreadGroup.duration' && threadGroupConfig.duration !== readThreadGroupConfig(threadGroupNode.value).duration) {
      return threadGroupConfig.duration
    }
    const properties = threadGroupNode.value?.properties || {}
    return Object.prototype.hasOwnProperty.call(properties, property) ? properties[property] : fallback
  }

  function canEditNumber(value: any, min: number, max: number) {
    const number = Number(value)
    return /^-?\d+$/.test(String(value)) && Number.isSafeInteger(number) && number >= min && number <= max
  }

  function readCsvConfig(node: any) {
    const p = node?.properties || {}
    return { filePath: p.filename ?? '', variableNames: p.variableNames ?? '', delimiter: p.delimiter ?? ',',
      encoding: p.encoding ?? 'UTF-8', ignoreFirstLine: p.ignoreFirstLine ?? true, recycle: p.recycle ?? true }
  }

  function unsupportedEdit(field: string): never {
    throw new Error(`无法安全保存 ${field}；原始计划未修改。请撤销此项修改，或使用右上角 JMX 编辑器（/editor/${planId.value}）编辑复杂配置。`)
  }

  function parseEndpoint(node: any): any {
    const props = node.properties || {}

    // Parse headers from child HeaderManager
    const headers: Array<{ name: string; value: string }> = []
    let preScript = { language: 'groovy', script: '' }
    let postScript = { language: 'groovy', script: '' }
    const assertions: AssertionRule[] = []
    const extractors: any[] = []

    if (node.children) {
      for (const child of node.children) {
        if (child.testclass === 'HeaderManager') {
          const hlist = child.properties?.['HeaderManager.headers'] || []
          for (const h of hlist) {
            headers.push({
              name: h['Header.name'] || '',
              value: h['Header.value'] || '',
            })
          }
        }
        if (child.testclass === 'JSR223PreProcessor') {
          preScript = {
            language: child.properties?.scriptLanguage || 'groovy',
            script: child.properties?.script || '',
          }
        }
        if (child.testclass === 'JSR223PostProcessor') {
          postScript = {
            language: child.properties?.scriptLanguage || 'groovy',
            script: child.properties?.script || '',
          }
        }
        if (child.testclass === 'ResponseAssertion') {
          const testField = child.properties?.['Assertion.test_field'] || 'Assertion.response_code'
          const testType = child.properties?.['Assertion.test_type']
          assertions.push({
            type: testField.includes('response_code') ? 'status_code' : 'body_contains',
            operator: testType === 16 ? 'neq' : 'eq',
            field: '',
            expected: '',
          })
        }
        if (child.testclass === 'JSONAssertion') {
          assertions.push({
            type: 'json_path',
            operator: 'eq',
            field: child.properties?.JSON_PATH || '',
            expected: child.properties?.EXPECTED_VALUE || '',
          })
        }
        if (child.testclass === 'JSONPostProcessor' || child.testclass === 'RegexExtractor' || child.testclass === 'BoundaryExtractor') {
          extractors.push({
            type: child.testclass === 'JSONPostProcessor' ? 'JSONPath' : child.testclass === 'RegexExtractor' ? 'Regex' : 'Boundary',
            refName: child.properties?.[child.testclass === 'JSONPostProcessor' ? 'JSONPostProcessor.referenceNames' : child.testclass === 'RegexExtractor' ? 'RegexExtractor.refname' : 'BoundaryExtractor.refname'] || '',
          })
        }
      }
    }

    // Parse body
    let bodyType: EndpointData['bodyType'] = 'none'
    let bodyContent = ''
    if (props['HTTPSampler.postBodyRaw'] && props['__body_data']) {
      bodyType = 'json'
      bodyContent = props['__body_data']
    }

    return {
      id: node.id || Math.random().toString(36).substring(2, 10),
      _rawTimeout: props['HTTPSampler.connect_timeout'] || 0,
      name: node.testname || t('common.unnamed'),
      description: '',
      method: props['HTTPSampler.method'] || 'GET',
      path: props['HTTPSampler.path'] || '/',
      domain: props['HTTPSampler.domain'] || '',
      port: props['HTTPSampler.port'] || '',
      protocol: props['HTTPSampler.protocol'] || '',
      headers,
      cookies: [],
      queryParams: (props['__http_params'] || []).map((p: any) => ({
        name: p['Argument.name'] || '', value: p['Argument.value'] || '', description: '',
      })),
      bodyType,
      bodyContent,
      auth: { type: 'none', username: '', password: '' },
      preScript,
      postScript,
      assertions,
      extractors,
      settings: {
        timeout: parseInt(props['HTTPSampler.connect_timeout'] || '0'),
        followRedirects: props['HTTPSampler.follow_redirects'] !== false,
        autoRedirects: props['HTTPSampler.auto_redirects'] === true,
        keepAlive: props['HTTPSampler.use_keepalive'] !== false,
        encoding: props['HTTPSampler.contentEncoding'] || '',
      },
    }
  }

  function buildTreeFromEndpoints(): any {
    const root = JSON.parse(JSON.stringify(treeData.value))
    if (!root) unsupportedEdit('未加载的测试计划')
    const originals = new Map<string, any>()
    const nodes = new Map<string, any>()
    const groups: any[] = []
    function index(node: any, original: any) {
      nodes.set(node.id, node)
      originals.set(node.id, original)
      if (isThreadGroup(node)) groups.push(node)
      for (const [i, child] of (node.children || []).entries()) index(child, original.children[i])
    }
    index(root, treeData.value)

    const tg = groups[0]
    if (tg) {
      patchProperties(tg, threadGroupConfig, readThreadGroupConfig(tg), {
        numThreads: 'ThreadGroup.num_threads', rampTime: 'ThreadGroup.ramp_time', loops: 'loops',
        duration: 'ThreadGroup.duration', delay: 'ThreadGroup.delay', scheduler: 'ThreadGroup.scheduler',
        onSampleError: 'ThreadGroup.on_sample_error',
      })
      const csvNodes = (tg.children || []).filter(isCsvDataset)
      if (JSON.stringify(csvDatasetConfig) !== JSON.stringify(readCsvConfig(csvNodes[0]))) {
        if (csvNodes.length > 1) unsupportedEdit('多个 CSV 数据集')
        if (!csvDatasetConfig.filePath) {
          tg.children = tg.children.filter((child: any) => child !== csvNodes[0])
        } else {
          const csv = csvNodes[0] || { id: Math.random().toString(36).substring(2, 10), testclass: 'CSVDataSet', testname: 'CSV Data Set Config', enabled: true, properties: {}, children: [] }
          patchProperties(csv, csvDatasetConfig, csvNodes[0] ? readCsvConfig(csv) : {}, {
            filePath: 'filename', variableNames: 'variableNames', delimiter: 'delimiter', encoding: 'encoding',
            ignoreFirstLine: 'ignoreFirstLine', recycle: 'recycle',
          })
          if (!csvNodes.length) (tg.children ||= []).push(csv)
        }
      }
    } else if (JSON.stringify(threadGroupConfig) !== JSON.stringify(readThreadGroupConfig(null)) ||
      JSON.stringify(csvDatasetConfig) !== JSON.stringify(readCsvConfig(null))) {
      unsupportedEdit('缺少线程组的配置')
    }

    // Modify existing samplers in place. Never rebuild or flatten their subtrees.
    function patchChildren(node: any) {
      if (!node.children) return
      node.children = node.children.filter((child: any) => child.testclass !== 'HTTPSamplerProxy' || endpoints.value.has(child.id))
      for (const child of node.children) {
        if (child.testclass === 'HTTPSamplerProxy') patchSamplerNode(child, endpoints.value.get(child.id), originals.get(child.id))
        patchChildren(child)
      }
    }
    patchChildren(root)

    for (const ep of endpoints.value.values()) {
      if (nodes.has(ep.id)) continue
      const parent = nodes.get(ep._parentId) || (groups.length === 1 ? tg : null)
      if (!parent) unsupportedEdit('新增接口的所属线程组；请先选择同组接口')
      const original = originals.get(ep._sourceId)
      let sampler: any
      if (original) {
        sampler = JSON.parse(JSON.stringify(original))
        const renewIds = (node: any) => {
          node.id = Math.random().toString(36).substring(2, 10)
          for (const child of node.children || []) {
            if (child.testclass === 'HTTPSamplerProxy') unsupportedEdit('包含嵌套请求的接口复制')
            renewIds(child)
          }
        }
        renewIds(sampler)
        sampler.id = ep.id
        patchSamplerNode(sampler, ep, original)
      } else {
        sampler = buildSamplerNode(ep)
        // The simple builder cannot represent these fields; don't silently ignore them.
        const empty = parseEndpoint({ id: ep.id, properties: {}, children: [] })
        for (const field of ['queryParams', 'cookies', 'auth', 'assertions', 'extractors', 'description']) {
          if (JSON.stringify(ep[field]) !== JSON.stringify(empty[field])) unsupportedEdit(field)
        }
        if (['form-data', 'x-www-form-urlencoded'].includes(ep.bodyType)) unsupportedEdit('表单请求体')
      }
      (parent.children ||= []).push(sampler)
    }
    const order = new Map(Array.from(endpoints.value.keys()).map((id, i) => [id, i]))
    function reorderChildren(node: any) {
      if (!node.children) return
      const samplers = node.children.filter((child: any) => child.testclass === 'HTTPSamplerProxy')
        .sort((a: any, b: any) => (order.get(a.id) ?? 0) - (order.get(b.id) ?? 0))
      node.children = node.children.map((child: any) => child.testclass === 'HTTPSamplerProxy' ? samplers.shift() : child)
      for (const child of node.children) reorderChildren(child)
    }
    reorderChildren(root)
    return root
  }

  function patchProperties(node: any, current: any, previous: any, fields: Record<string, string>) {
    for (const [field, property] of Object.entries(fields)) {
      if (JSON.stringify(current[field]) !== JSON.stringify(previous[field])) {
        (node.properties ||= {})[property] = current[field]
      }
    }
  }

  function patchSamplerNode(node: any, ep: any, original: any) {
    const previous = parseEndpoint(original)
    // These UI models omit JMeter options. Retain them unless edited; then require the full editor.
    for (const field of ['queryParams', 'cookies', 'auth', 'assertions', 'extractors', 'description']) {
      if (JSON.stringify(ep[field]) !== JSON.stringify(previous[field])) unsupportedEdit(field)
    }
    if (ep.name !== previous.name) node.testname = ep.name
    patchProperties(node, ep, previous, { method: 'HTTPSampler.method', path: 'HTTPSampler.path', domain: 'HTTPSampler.domain', port: 'HTTPSampler.port', protocol: 'HTTPSampler.protocol' })
    patchProperties(node, ep.settings, previous.settings, {
      timeout: 'HTTPSampler.connect_timeout', followRedirects: 'HTTPSampler.follow_redirects',
      autoRedirects: 'HTTPSampler.auto_redirects', keepAlive: 'HTTPSampler.use_keepalive', encoding: 'HTTPSampler.contentEncoding',
    })
    if (ep.bodyType !== previous.bodyType || ep.bodyContent !== previous.bodyContent) {
      if (original.properties?.__http_params?.length || original.properties?.['HTTPsampler.Arguments'] ||
        ['form-data', 'x-www-form-urlencoded'].includes(ep.bodyType)) unsupportedEdit('参数/表单请求体转换')
      if (ep.bodyType !== previous.bodyType) {
        node.properties['HTTPSampler.postBodyRaw'] = ep.bodyType !== 'none'
        if ('HTTPSampler.DO_MULTIPART_POST' in node.properties) node.properties['HTTPSampler.DO_MULTIPART_POST'] = false
      }
      if (ep.bodyType === 'none') delete node.properties.__body_data
      else node.properties.__body_data = ep.bodyContent
    }
    for (const [field, testclass] of [['headers', 'HeaderManager'], ['preScript', 'JSR223PreProcessor'], ['postScript', 'JSR223PostProcessor']]) {
      if (JSON.stringify(ep[field]) === JSON.stringify(previous[field])) continue
      const matches = (node.children || []).filter((child: any) => child.testclass === testclass)
      if (matches.length > 1) unsupportedEdit(`多个 ${testclass}`)
      let child = matches[0]
      if (!child) {
        child = { id: Math.random().toString(36).substring(2, 10), testclass, testname: testclass, enabled: true, properties: {}, children: [] }
        node.children ||= []
        node.children.push(child)
      }
      const properties = child.properties ||= {}
      if (field === 'headers') {
        const oldHeaders = properties['HeaderManager.headers'] || []
        if (oldHeaders.length !== ep.headers.length && oldHeaders.length) unsupportedEdit('请求头集合增删')
        properties['HeaderManager.headers'] = ep.headers.map((h: any, i: number) => ({
          ...(oldHeaders[i] || { _elementType: 'Header', _name: h.name }), 'Header.name': h.name, 'Header.value': h.value,
        }))
      } else {
        if (properties.filename) unsupportedEdit('外部文件脚本')
        patchProperties(child, ep[field], matches.length ? previous[field] : {}, { language: 'scriptLanguage', script: 'script' })
      }
    }
  }

  function buildSamplerNode(ep: any): any {
    const props: Record<string, any> = {
      'HTTPSampler.method': ep.method,
      'HTTPSampler.path': ep.path,
      'HTTPSampler.domain': ep.domain,
      'HTTPSampler.port': ep.port,
      'HTTPSampler.protocol': ep.protocol,
      'HTTPSampler.contentEncoding': ep.settings?.encoding || '',
      'HTTPSampler.follow_redirects': ep.settings?.followRedirects !== false,
      'HTTPSampler.auto_redirects': ep.settings?.autoRedirects === true,
      'HTTPSampler.use_keepalive': ep.settings?.keepAlive !== false,
      'HTTPSampler.connect_timeout': String(ep.settings?.timeout || ''),
      'HTTPSampler.postBodyRaw': ep.bodyType !== 'none',
      'HTTPSampler.DO_MULTIPART_POST': ep.bodyType === 'form-data',
    }

    if (ep.bodyType !== 'none' && ep.bodyContent) {
      props['__body_data'] = ep.bodyContent
    }

    const children: any[] = []

    // Headers
    if (ep.headers && ep.headers.length > 0) {
      children.push({
        id: Math.random().toString(36).substring(2, 10),
        testclass: 'HeaderManager',
        testname: 'HTTP Header Manager',
        enabled: true,
        properties: {
          'HeaderManager.headers': ep.headers.map((h: any) => ({
            _elementType: 'Header',
            _name: h.name || '',
            'Header.name': h.name || '',
            'Header.value': h.value || '',
          })),
        },
        children: [],
      })
    }

    // Pre-processor script
    if (ep.preScript && ep.preScript.script) {
      children.push({
        id: Math.random().toString(36).substring(2, 10),
        testclass: 'JSR223PreProcessor',
        testname: 'PreProcessor',
        enabled: true,
        properties: {
          scriptLanguage: ep.preScript.language || 'groovy',
          script: ep.preScript.script,
          parameters: '',
          filename: '',
        },
        children: [],
      })
    }

    // Post-processor script
    if (ep.postScript && ep.postScript.script) {
      children.push({
        id: Math.random().toString(36).substring(2, 10),
        testclass: 'JSR223PostProcessor',
        testname: 'PostProcessor',
        enabled: true,
        properties: {
          scriptLanguage: ep.postScript.language || 'groovy',
          script: ep.postScript.script,
          parameters: '',
          filename: '',
        },
        children: [],
      })
    }

    // Assertions
    if (ep.assertions) {
      for (const a of ep.assertions) {
        if (a.type === 'json_path') {
          children.push({
            id: Math.random().toString(36).substring(2, 10),
            testclass: 'JSONAssertion',
            testname: 'JSON Assertion',
            enabled: true,
            properties: {
              JSON_PATH: a.field || '',
              EXPECTED_VALUE: a.expected || '',
              JSONVALIDATION: false,
              EXPECT_NULL: false,
              INVERT: a.operator === 'neq',
              ISREGEX: a.operator === 'regex',
            },
            children: [],
          })
        } else if (a.type === 'duration') {
          children.push({
            id: Math.random().toString(36).substring(2, 10),
            testclass: 'DurationAssertion',
            testname: 'Duration Assertion',
            enabled: true,
            properties: {
              'DurationAssertion.duration': String(a.expected || '1000'),
            },
            children: [],
          })
        } else if (a.type === 'size') {
          const operatorMap: Record<string, number> = { eq: 1, neq: 2, gt: 3, lt: 4, gte: 5, lte: 6 }
          children.push({
            id: Math.random().toString(36).substring(2, 10),
            testclass: 'SizeAssertion',
            testname: 'Size Assertion',
            enabled: true,
            properties: {
              'SizeAssertion.size': String(a.expected || '1024'),
              'SizeAssertion.operator': operatorMap[a.operator] || 3,
            },
            children: [],
          })
        } else if (a.type === 'jsr223') {
          children.push({
            id: Math.random().toString(36).substring(2, 10),
            testclass: 'JSR223Assertion',
            testname: 'JSR223 Assertion',
            enabled: true,
            properties: {
              scriptLanguage: a.language || 'groovy',
              script: a.expected || '',
              parameters: a.parameters || '',
              filename: a.filename || '',
            },
            children: [],
          })
        } else {
          let testField = 'Assertion.response_code'
          if (a.type === 'body_contains' || a.type === 'body_regex') testField = 'Assertion.response_data'
          else if (a.type === 'header') testField = 'Assertion.response_headers'
          else if (a.type === 'response_time') testField = 'Assertion.response_data'

          let testType = 8 // Substring/contains
          if (a.operator === 'eq') testType = 4
          else if (a.operator === 'neq') testType = 16
          else if (a.operator === 'regex') testType = 2

          children.push({
            id: Math.random().toString(36).substring(2, 10),
            testclass: 'ResponseAssertion',
            testname: 'Response Assertion',
            enabled: true,
            properties: {
              'Assertion.test_field': testField,
              'Assertion.test_type': testType,
              'Assertion.assume_success': false,
              'Assertion.custom_message': '',
            },
            children: [],
          })
        }
      }
    }

    // Extractors
    if (ep.extractors) {
      for (const ext of ep.extractors) {
        if (ext.type === 'JSONPath') {
          children.push({
            id: Math.random().toString(36).substring(2, 10),
            testclass: 'JSONPostProcessor',
            testname: 'JSON Extractor',
            enabled: true,
            properties: {
              'JSONPostProcessor.referenceNames': ext.refName || '',
              'JSONPostProcessor.jsonPathExprs': ext.config?.jsonPath || '',
              'JSONPostProcessor.match_numbers': String(ext.config?.matchNo || '1'),
              'JSONPostProcessor.defaultValues': ext.config?.defaultValue || 'NOT_FOUND',
            },
            children: [],
          })
        } else if (ext.type === 'Regex') {
          children.push({
            id: Math.random().toString(36).substring(2, 10),
            testclass: 'RegexExtractor',
            testname: 'Regex Extractor',
            enabled: true,
            properties: {
              'RegexExtractor.useHeaders': false,
              'RegexExtractor.refname': ext.refName || '',
              'RegexExtractor.regex': ext.config?.regex || '',
              'RegexExtractor.template': ext.config?.template || '$1$',
              'RegexExtractor.match_number': String(ext.config?.matchNo || '1'),
              'RegexExtractor.default': ext.config?.defaultValue || 'NOT_FOUND',
            },
            children: [],
          })
        } else if (ext.type === 'Boundary') {
          children.push({
            id: Math.random().toString(36).substring(2, 10),
            testclass: 'BoundaryExtractor',
            testname: 'Boundary Extractor',
            enabled: true,
            properties: {
              'BoundaryExtractor.useHeaders': false,
              'BoundaryExtractor.refname': ext.refName || '',
              'BoundaryExtractor.lboundary': ext.config?.left || '',
              'BoundaryExtractor.rboundary': ext.config?.right || '',
              'BoundaryExtractor.match_number': String(ext.config?.matchNo || '1'),
              'BoundaryExtractor.default': ext.config?.defaultValue || 'NOT_FOUND',
            },
            children: [],
          })
        } else if (ext.type === 'XPath') {
          children.push({
            id: Math.random().toString(36).substring(2, 10),
            testclass: 'XPath2Extractor',
            testname: 'XPath Extractor',
            enabled: true,
            properties: {
              'XPath2Extractor.refname': ext.refName || '',
              'XPath2Extractor.xpathQuery': ext.config?.xpathQuery || '',
              'XPath2Extractor.matchNumber': String(ext.config?.matchNo || '1'),
              'XPath2Extractor.defaultValue': ext.config?.defaultValue || 'NOT_FOUND',
              'XPath2Extractor.namespaces': ext.config?.namespaces || '',
            },
            children: [],
          })
        }
      }
    }

    return {
      id: ep.id,
      testclass: 'HTTPSamplerProxy',
      testname: ep.name,
      enabled: true,
      properties: props,
      children,
    }
  }

  return {
    isThreadGroup, isCsvDataset, readThreadGroupConfig, threadGroupNumberValue, canEditNumber, readCsvConfig,
    parseEndpoint, buildTreeFromEndpoints,
  }
}
