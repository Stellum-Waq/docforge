/**
 * CDP 驱动的 UI 探针（开发工具）。
 *
 * ## 为什么需要它
 * 用「屏幕截图 + SendKeys 模拟键盘」验证桌面应用是**危险**的：
 * 只要 SetForegroundWindow 没生效，按键就会打进用户当时正在用的其他程序
 * （本项目的开发过程中确实误操作过一次微信）。而且抓屏抓到的也可能是别的窗口，
 * 导致验证结论完全错误 —— 这正是发生过的事。
 *
 * 正确做法是走 Chrome DevTools Protocol：直接和 Electron 渲染进程通信，
 * 既能精确操作 DOM，又只能截到本应用自己的画面，绝不影响其他软件。
 *
 * ## 前置条件
 * 应用需要以调试端口启动：
 *     electron.exe --remote-debugging-port=9222 .
 *
 * ## 用法
 *     node tools/cdp.mjs shot  <输出路径>
 *     node tools/cdp.mjs click "<按钮文字>"
 *     node tools/cdp.mjs text                    # 导出页面可见文本
 *     node tools/cdp.mjs eval  "<js 表达式>"
 */

const PORT = process.env.DOCFORGE_CDP_PORT ?? '9222'

async function findPageTarget() {
  const res = await fetch(`http://127.0.0.1:${PORT}/json`)
  const targets = await res.json()
  const page = targets.find((t) => t.type === 'page' && t.webSocketDebuggerUrl)
  if (!page) {
    throw new Error(
      `未找到页面调试目标。确认应用是用 --remote-debugging-port=${PORT} 启动的。\n` +
        `当前目标：${targets.map((t) => `${t.type}:${t.title}`).join(', ') || '(空)'}`
    )
  }
  return page
}

async function connect() {
  const page = await findPageTarget()
  const ws = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => {
    ws.onopen = resolve
    ws.onerror = (e) => reject(new Error(`WebSocket 连接失败：${e.message ?? e.type}`))
  })

  let seq = 0
  const pending = new Map()

  ws.onmessage = (event) => {
    let msg
    try {
      msg = JSON.parse(event.data)
    } catch {
      return
    }
    const resolver = pending.get(msg.id)
    if (resolver) {
      pending.delete(msg.id)
      resolver(msg)
    }
  }

  const send = (method, params = {}) =>
    new Promise((resolve) => {
      const id = ++seq
      pending.set(id, resolve)
      ws.send(JSON.stringify({ id, method, params }))
    })

  return { ws, send }
}

async function evaluate(send, expression) {
  const result = await send('Runtime.evaluate', {
    expression,
    returnByValue: true,
    awaitPromise: true
  })
  const details = result.result
  if (details?.exceptionDetails) {
    throw new Error(`页面内求值出错：${details.exceptionDetails.text} ${details.result?.description ?? ''}`)
  }
  return details?.result?.value
}

/** 按可见文字点击元素。先精确匹配，再退回包含匹配。 */
function clickScript(text) {
  return `(() => {
    const want = ${JSON.stringify(text)};
    const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
    const visible = nodes.filter(n => n.offsetParent !== null);

    let target = visible.find(n => (n.innerText || '').trim() === want);
    if (!target) {
      target = visible.find(n => (n.innerText || '').trim().split('\\n').some(line => line.trim() === want));
    }
    if (!target) {
      target = visible.find(n => (n.innerText || '').includes(want));
    }
    if (!target) {
      return { ok: false, candidates: visible.map(n => (n.innerText || '').trim()).filter(Boolean).slice(0, 40) };
    }
    target.click();
    return { ok: true, clicked: (target.innerText || '').trim() };
  })()`
}

async function main() {
  const [command, ...rest] = process.argv.slice(2)
  const { ws, send } = await connect()

  // 关键：先把窗口带到前台。
  // 窗口被遮挡时 Chromium 会把页面标记为 hidden 并**完全暂停 rAF**，
  // 依赖动画的操作会表现成"点了没反应"，截图也可能拿到未刷新的画面。
  await send('Page.bringToFront')

  try {
    switch (command) {
      case 'shot': {
        const path = rest[0]
        if (!path) throw new Error('用法：shot <输出路径>')
        const result = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false })
        const data = result.result?.data
        if (!data) throw new Error(`截图失败：${JSON.stringify(result).slice(0, 300)}`)
        const { writeFile } = await import('node:fs/promises')
        await writeFile(path, Buffer.from(data, 'base64'))
        console.log(`已保存截图：${path}`)
        break
      }

      case 'click': {
        const text = rest.join(' ')
        const outcome = await evaluate(send, clickScript(text))
        if (!outcome?.ok) {
          console.log(`未找到可点击元素「${text}」`)
          console.log('当前可点击元素：')
          for (const item of outcome?.candidates ?? []) console.log(`  · ${item.replace(/\n/g, ' / ')}`)
          process.exitCode = 1
        } else {
          console.log(`已点击：${outcome.clicked.replace(/\n/g, ' / ')}`)
        }
        break
      }

      case 'text': {
        const text = await evaluate(send, `document.body.innerText`)
        console.log(text)
        break
      }

      case 'img': {
        // 导出页面上第一张 data URL 图片（例如服务端渲染的预览图）。
        // 用于把"预览到底长什么样"落盘做视觉检查，不必再去截屏。
        const path = rest[0]
        if (!path) throw new Error('用法：img <输出路径> [索引]')
        const index = Number(rest[1] ?? 0)
        const dataUrl = await evaluate(
          send,
          `(Array.from(document.querySelectorAll('img')).filter(i => i.src.startsWith('data:image'))[${index}] || {}).src || ''`
        )
        if (!dataUrl) throw new Error('页面上没有 data URL 图片')
        const base64 = String(dataUrl).split(',')[1]
        const { writeFile } = await import('node:fs/promises')
        await writeFile(path, Buffer.from(base64, 'base64'))
        console.log(`已保存页面图片：${path}（${Buffer.from(base64, 'base64').length} 字节）`)
        break
      }

      case 'eval': {
        const value = await evaluate(send, rest.join(' '))
        console.log(typeof value === 'string' ? value : JSON.stringify(value, null, 2))
        break
      }

      default:
        console.log(
          '用法：\n' +
            '  node tools/cdp.mjs shot  <输出路径>\n' +
            '  node tools/cdp.mjs img   <输出路径> [索引]   # 导出页面上的 data URL 图片\n' +
            '  node tools/cdp.mjs click "<按钮文字>"\n' +
            '  node tools/cdp.mjs text\n' +
            '  node tools/cdp.mjs eval  "<js 表达式>"'
        )
        process.exitCode = 1
    }
  } finally {
    ws.close()
  }
}

main().catch((err) => {
  console.error(err.message)
  process.exitCode = 1
})
