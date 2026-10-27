/* Keep live data updates separate from the reader's selection and scroll state. */
"use strict"

window.MonitorLiveDOM = (() => {
  const pendingHTML = new Map()
  const pendingText = new Map()
  const lastHTML = new WeakMap()
  const anchorSelector = "pre, summary, tr, [data-key]"
  let pointerTarget = null
  let frame = null

  function isReading(node) {
    if (
      pointerTarget &&
      (node.contains(pointerTarget) || pointerTarget.contains(node))
    ) {
      return true
    }
    const selection = document.getSelection()
    if (!selection || selection.isCollapsed) {
      return false
    }
    for (let index = 0; index < selection.rangeCount; index += 1) {
      if (selection.getRangeAt(index).intersectsNode(node)) {
        return true
      }
    }
    return false
  }

  function isPinned(node) {
    return isReading(node) || node.contains(document.activeElement)
  }

  function key(node) {
    if (node.nodeType !== Node.ELEMENT_NODE) {
      return null
    }
    if (node.hasAttribute("data-key")) {
      return node.tagName + ":" + node.dataset.key
    }
    if (node.hasAttribute("data-task-id")) {
      return node.tagName + ":" + node.dataset.taskId
    }
    return null
  }

  function compatible(current, next) {
    return (
      current.nodeType === next.nodeType && current.nodeName === next.nodeName
    )
  }

  function patchNode(current, next) {
    if (current.nodeType === Node.TEXT_NODE) {
      if (current.data === next.data) {
        return false
      }
      if (isReading(current)) {
        return true
      }
      current.data = next.data
      return false
    }
    if (current.nodeType !== Node.ELEMENT_NODE) {
      return false
    }
    for (const attribute of [...current.attributes]) {
      if (attribute.name === "open" && current.tagName === "DETAILS") {
        continue
      }
      if (!next.hasAttribute(attribute.name)) {
        current.removeAttribute(attribute.name)
      }
    }
    for (const attribute of next.attributes) {
      if (attribute.name === "open" && current.tagName === "DETAILS") {
        continue
      }
      if (current.getAttribute(attribute.name) !== attribute.value) {
        current.setAttribute(attribute.name, attribute.value)
      }
    }
    return patchChildren(current, next)
  }

  function patchChildren(parent, next) {
    const previous = [...parent.childNodes]
    const keyed = new Map()
    for (const node of previous) {
      const id = key(node)
      if (id !== null) {
        keyed.set(id, node)
      }
    }
    const used = new Set()
    const desired = []
    let blocked = false
    for (const incoming of next.childNodes) {
      const id = key(incoming)
      let current = keyed.get(id)
      if (id === null) {
        current = previous.find((node) => {
          return (
            !used.has(node) && key(node) === null && compatible(node, incoming)
          )
        })
      }
      if (current && compatible(current, incoming) && !used.has(current)) {
        used.add(current)
        blocked = patchNode(current, incoming) || blocked
        desired.push(current)
      } else {
        desired.push(incoming.cloneNode(true))
      }
    }
    let cursor = parent.firstChild
    for (const node of desired) {
      if (node === cursor) {
        cursor = cursor.nextSibling
      } else if (node.parentNode === parent && isPinned(node)) {
        blocked = true
        cursor = node.nextSibling
      } else {
        parent.insertBefore(node, cursor)
      }
    }
    for (const node of previous) {
      if (!used.has(node)) {
        if (isPinned(node)) {
          blocked = true
        } else {
          node.remove()
        }
      }
    }
    return blocked
  }

  function readingPosition(root) {
    const positions = []
    for (const node of root.querySelectorAll("*")) {
      if (node.scrollTop || node.scrollLeft) {
        positions.push([node, node.scrollTop, node.scrollLeft])
      }
    }
    let scroller = document.scrollingElement
    for (let node = root; node; node = node.parentElement) {
      positions.push([node, node.scrollTop, node.scrollLeft])
      if (
        scroller === document.scrollingElement &&
        node.scrollHeight > node.clientHeight &&
        /auto|scroll/.test(getComputedStyle(node).overflowY)
      ) {
        scroller = node
      }
    }
    const bounds = scroller.getBoundingClientRect()
    let top = 0
    let bottom = innerHeight
    if (scroller !== document.scrollingElement) {
      top = bounds.top
      bottom = bounds.bottom
    }
    let anchor = null
    if (scroller.scrollTop) {
      function visible(node) {
        const rect = node.getBoundingClientRect()
        return rect.height && rect.bottom > top && rect.top < bottom
      }
      anchor = [...root.querySelectorAll(anchorSelector)].find(visible)
      if (!anchor && scroller === document.scrollingElement) {
        anchor = [...document.querySelectorAll(anchorSelector)].find(visible)
      }
    }
    let anchorTop = null
    if (anchor) {
      anchorTop = anchor.getBoundingClientRect().top
    }
    return function restore() {
      for (const [node, scrollTop, scrollLeft] of positions) {
        node.scrollTop = scrollTop
        node.scrollLeft = scrollLeft
      }
      if (anchor?.isConnected) {
        scroller.scrollTop += anchor.getBoundingClientRect().top - anchorTop
      }
    }
  }

  function renderHTML(root, html) {
    if (lastHTML.get(root) === html && !pendingHTML.has(root)) {
      return
    }
    const template = document.createElement("template")
    // Use the actual parent context so tbody rows remain table rows.
    const range = document.createRange()
    range.selectNodeContents(root)
    template.content.append(range.createContextualFragment(html))
    const restore = readingPosition(root)
    const blocked = patchChildren(root, template.content)
    restore()
    if (blocked) {
      pendingHTML.set(root, html)
    } else {
      pendingHTML.delete(root)
      lastHTML.set(root, html)
    }
  }

  function setText(node, text) {
    const value = String(text)
    if (node.textContent === value) {
      pendingText.delete(node)
    } else if (isReading(node)) {
      pendingText.set(node, value)
    } else {
      node.textContent = value
      pendingText.delete(node)
    }
  }

  function flush() {
    frame = null
    for (const [node, text] of pendingText) {
      if (node.isConnected) {
        setText(node, text)
      } else {
        pendingText.delete(node)
      }
    }
    for (const [root, html] of pendingHTML) {
      if (root.isConnected) {
        renderHTML(root, html)
      } else {
        pendingHTML.delete(root)
      }
    }
  }

  function scheduleFlush() {
    if (frame === null) {
      frame = requestAnimationFrame(flush)
    }
  }

  document.addEventListener(
    "pointerdown",
    (event) => {
      pointerTarget = event.target
    },
    true,
  )
  function releasePointer() {
    pointerTarget = null
    scheduleFlush()
  }
  window.addEventListener("pointerup", releasePointer)
  window.addEventListener("pointercancel", releasePointer)
  window.addEventListener("blur", releasePointer)
  document.addEventListener("selectionchange", scheduleFlush)
  document.addEventListener("focusout", scheduleFlush)
  return { renderHTML, setText }
})()
