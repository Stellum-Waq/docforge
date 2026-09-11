import type { DocForgeBridge } from '@shared/index'

declare global {
  interface Window {
    docforge: DocForgeBridge
  }
}

export {}
