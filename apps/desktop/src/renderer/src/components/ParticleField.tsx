import { useEffect, useRef } from 'react'

interface Particle {
  x: number
  y: number
  vx: number
  vy: number
  r: number
  hue: number
}

interface ParticleFieldProps {
  /** 粒子数量。默认按画布面积自适应 */
  density?: number
  className?: string
}

/**
 * 轻量 Canvas 粒子场背景。
 *
 * 刻意不使用 three.js —— 一个 WebGL 库会给包体增加约 600KB，
 * 而这里只需要 2D 连线粒子，Canvas 2D 已经足够且更省电（设计文档 §2.5 性能纪律）。
 *
 * 另外做了三件保证流畅度的事：
 *  1. 用 devicePixelRatio 适配高分屏，但上限 2 避免 4K 屏上过度绘制
 *  2. 页面隐藏时暂停 rAF，不浪费后台 CPU
 *  3. 尊重 prefers-reduced-motion：开启时只画静态星点，不做动画
 */
export function ParticleField({ density = 0.00008, className }: ParticleFieldProps): React.JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d', { alpha: true })
    if (!ctx) return

    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches

    let width = 0
    let height = 0
    let dpr = 1
    let particles: Particle[] = []
    let raf = 0
    let running = true

    const seed = (): void => {
      const count = Math.max(28, Math.min(120, Math.round(width * height * density)))
      particles = Array.from({ length: count }, () => ({
        x: Math.random() * width,
        y: Math.random() * height,
        vx: (Math.random() - 0.5) * 0.18,
        vy: (Math.random() - 0.5) * 0.18,
        r: Math.random() * 1.6 + 0.4,
        // 色相锁定在青→紫区间（180°–285°），与极光主色一致
        hue: 180 + Math.random() * 105
      }))
    }

    const resize = (): void => {
      const rect = canvas.getBoundingClientRect()
      dpr = Math.min(window.devicePixelRatio || 1, 2)
      width = rect.width
      height = rect.height
      canvas.width = Math.max(1, Math.floor(width * dpr))
      canvas.height = Math.max(1, Math.floor(height * dpr))
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      seed()
    }

    const draw = (): void => {
      ctx.clearRect(0, 0, width, height)

      // 连线：距离越近越亮，形成"数据流"的观感
      const maxDist = 130
      for (let i = 0; i < particles.length; i++) {
        const a = particles[i]
        for (let j = i + 1; j < particles.length; j++) {
          const b = particles[j]
          const dx = a.x - b.x
          const dy = a.y - b.y
          const dist = Math.hypot(dx, dy)
          if (dist > maxDist) continue
          const alpha = (1 - dist / maxDist) * 0.16
          ctx.strokeStyle = `hsla(${(a.hue + b.hue) / 2}, 85%, 65%, ${alpha})`
          ctx.lineWidth = 0.6
          ctx.beginPath()
          ctx.moveTo(a.x, a.y)
          ctx.lineTo(b.x, b.y)
          ctx.stroke()
        }
      }

      for (const p of particles) {
        ctx.fillStyle = `hsla(${p.hue}, 90%, 70%, 0.75)`
        ctx.beginPath()
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2)
        ctx.fill()
      }
    }

    const step = (): void => {
      if (!running) return
      for (const p of particles) {
        p.x += p.vx
        p.y += p.vy
        // 环绕边界，避免粒子在边缘堆积
        if (p.x < -10) p.x = width + 10
        if (p.x > width + 10) p.x = -10
        if (p.y < -10) p.y = height + 10
        if (p.y > height + 10) p.y = -10
      }
      draw()
      raf = requestAnimationFrame(step)
    }

    const onVisibility = (): void => {
      if (document.hidden) {
        running = false
        cancelAnimationFrame(raf)
      } else if (!reduceMotion) {
        running = true
        raf = requestAnimationFrame(step)
      }
    }

    resize()
    if (reduceMotion) {
      draw() // 静态渲染一次即可
    } else {
      raf = requestAnimationFrame(step)
    }

    const observer = new ResizeObserver(resize)
    observer.observe(canvas)
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      running = false
      cancelAnimationFrame(raf)
      observer.disconnect()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [density])

  return <canvas ref={canvasRef} className={className} aria-hidden="true" />
}
