// 统一的 fetch 封装：后端所有接口都返回 JSON，出错时带 {ok:false, message}。
// 同源请求会自动带上浏览器已有的 Basic Auth 凭据，这里不用处理登录。
export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message)
    this.status = status
    this.payload = payload
  }
}

async function request(method, url, body) {
  const init = { method, credentials: 'same-origin', headers: {} }
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(body)
  }
  let res
  try {
    res = await fetch(url, init)
  } catch (e) {
    throw new ApiError('网络请求失败，请检查服务器是否可达', 0, null)
  }
  let payload = null
  try {
    payload = await res.json()
  } catch (e) {
    /* 非 JSON 响应，下面按状态码报错 */
  }
  if (!res.ok || (payload && payload.ok === false)) {
    throw new ApiError((payload && payload.message) || `请求失败（HTTP ${res.status}）`, res.status, payload)
  }
  return payload
}

export const get = (url) => request('GET', url)
export const post = (url, body = {}) => request('POST', url, body)

// 下载二进制（备份归档）：POST 后把响应存成文件。
export async function downloadPost(url) {
  let res
  try {
    res = await fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: '{}' })
  } catch (e) {
    throw new ApiError('网络请求失败，请检查服务器是否可达', 0, null)
  }
  if (!res.ok) {
    let message = `下载失败（HTTP ${res.status}）`
    try { message = (await res.json()).message || message } catch (e) { /* ignore */ }
    throw new ApiError(message, res.status, null)
  }
  const disposition = res.headers.get('Content-Disposition') || ''
  const name = (/filename="?([^";]+)"?/.exec(disposition) || [])[1] || 'download.bin'
  const blob = await res.blob()
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(a.href), 10000)
  return name
}
