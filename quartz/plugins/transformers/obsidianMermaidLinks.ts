import { QuartzTransformerPlugin } from "../types"
import { visit } from "unist-util-visit"

/**
 * Converts [[wikilink#heading]] URLs inside Mermaid click directives to plain
 * relative URLs for the published Quartz site.
 *
 * Source .md (what Obsidian sees):
 *   click PRE "[[bug-verification-and-filing-revised#before-you-open-any-tool]]"
 *
 * Built HTML (what the browser gets):
 *   click PRE "bug-verification-and-filing-revised#before-you-open-any-tool"
 *
 * Runs only on code blocks with lang="mermaid" so ordinary wiki-links in
 * markdown text are unaffected and continue to be handled by ObsidianFlavoredMarkdown.
 */
export const ObsidianMermaidLinks: QuartzTransformerPlugin = () => {
  return {
    name: "ObsidianMermaidLinks",
    markdownPlugins() {
      return [
        () => (tree) => {
          visit(tree, "code", (node: any) => {
            if (node.lang !== "mermaid") return
            // Strip [[ and ]] from quoted wiki-link click URLs, leaving a
            // plain relative URL that works in any browser.
            node.value = node.value.replace(/"(\[\[([^\]]+)\]\])"/g, '"$2"')
          })
        },
      ]
    },
  }
}
