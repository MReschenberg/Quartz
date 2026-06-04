import { QuartzConfig } from "./quartz/cfg"
import * as Plugin from "./quartz/plugins"

/**
 * Quartz 4 Configuration
 *
 * See https://quartz.jzhao.xyz/configuration for more information.
 */
const config: QuartzConfig = {
  configuration: {
    pageTitle: "Morgan's Digital Garden 🪴",
    pageTitleSuffix: "",
    enableSPA: true,
    enablePopovers: true,
    analytics: {
      provider: "plausible",
    },
    locale: "en-US",
    baseUrl: "mreschenberg.github.io/Quartz",
    ignorePatterns: ["private", "tempaltes", "Attachments/!(public-|Public-)*.*", "Templates"],
    defaultDateType: "modified",
    theme: {
      fontOrigin: "googleFonts",
      cdnCaching: true,
      typography: {
        header: "Kode Mono",
        body: "Atkinson Hyperlegible",
        code: "Atkinson Hyperlegible Mono",
      },
      colors: {
        lightMode: {
          light: "#fbf1c7",
          lightgray: "#d5c4a1",
          gray: "#bdae93",
          darkgray: "#3c3836",
          dark: "#282828",
          secondary: "#af3a03",
          tertiary: "#d65d0e",
          highlight: "rgba(215, 153, 33, 0.15)",
          textHighlight: "#d7992188",
        },
        darkMode: {
          light: "#282828",
          lightgray: "#504945",
          gray: "#665c54",
          darkgray: "#ebdbb2",
          dark: "#fbf1c7",
          secondary: "#fb4934",
          tertiary: "#fe8019",
          highlight: "rgba(250, 189, 47, 0.15)",
          textHighlight: "#fabd2f88",
        },
      },
    },
  },
  plugins: {
    transformers: [
      Plugin.FrontMatter(),
      Plugin.CreatedModifiedDate({
        priority: ["frontmatter", "git", "filesystem"],
      }),
      Plugin.SyntaxHighlighting({
        theme: {
          light: "github-light",
          dark: "github-dark",
        },
        keepBackground: false,
      }),
      Plugin.ObsidianMermaidLinks(),
      Plugin.ObsidianFlavoredMarkdown({ enableInHtmlEmbed: false }),
      Plugin.GitHubFlavoredMarkdown(),
      Plugin.TableOfContents(),
      Plugin.CrawlLinks({ markdownLinkResolution: "shortest" }),
      Plugin.Description(),
      Plugin.Latex({ renderEngine: "katex" }),
    ],
    filters: [Plugin.ExplicitPublish()],
    emitters: [
      Plugin.AliasRedirects(),
      Plugin.ComponentResources(),
      Plugin.ContentPage(),
      Plugin.FolderPage(),
      Plugin.TagPage(),
      Plugin.ContentIndex({
        enableSiteMap: true,
        enableRSS: true,
      }),
      Plugin.Assets(),
      Plugin.Static(),
      Plugin.Favicon(),
      Plugin.NotFoundPage(),
      // Comment out CustomOgImages to speed up build time
      Plugin.CustomOgImages(),
    ],
  },
}

export default config
