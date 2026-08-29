declare const __DEV__: boolean
/** Extension name, defined in packageJson.name */
declare const __NAME__: string
declare const browser: typeof import('webextension-polyfill')

declare module '*.vue' {
  const component: any
  export default component
}
