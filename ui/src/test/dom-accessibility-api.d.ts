// The package ships types its "exports" map hides from bundler resolution.
declare module "dom-accessibility-api" {
  export function computeAccessibleName(el: Element): string;
}
