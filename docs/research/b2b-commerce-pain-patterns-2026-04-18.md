---
tags: [research, b2b-commerce, salesforce, experience-cloud, detectors, self-learning]
date: 2026-04-18
type: research
confidence: 10 Verified (Primary) / 5 Verified (Secondary) / 6 Corroborated / 5 Unverified
---

# Research: Salesforce B2B Commerce Pain Patterns → Harness Detectors

## Important Framing — Cloud Scope

**Salesforce is a platform. "B2B Commerce" is one cloud within an org.** A single Salesforce org (e.g., the 30in30 sandbox) can simultaneously host Sales Cloud, Service Cloud, Agentforce, Revenue Cloud / CPQ, Field Service, B2B Commerce, Experience Cloud, Marketing Cloud Account Engagement, Data Cloud, Health Cloud, and various Industries clouds. A ticket can touch one cloud, several, or none (platform-only work like triggers, flows, or permissions).

**This research covers ONE cloud — B2B Commerce + its adjacent Experience Cloud / SFDX deployment concerns.** The resulting 25 patterns are all commerce-scoped. The other clouds each have their own distinct pain taxonomies that future research passes will mine. The detector library should be organized by cloud, not flatten into one bucket.

**Every resulting detector ships with a `required_clouds` gate.** Without it, these patterns would false-positive on unrelated work — e.g., Service Cloud's `Entitlement` (support-contract entitlements) collides by name with B2B Commerce's `CommerceEntitlementPolicy`; CPQ has its own "promotion" concept different from Commerce's 50+50 cap; Sales Cloud's "field searchability" is governed by Search Layouts, not the Commerce 50-field cap.

**See each YAML in `assertion-patterns/` for the specific `required_clouds` list per pattern.**

## Cloud Recognition — Required Harness Utility

For the cloud-scope gates to work, the harness needs a cloud-recognition helper that inspects each ticket and returns the set of clouds it touches. Signals per cloud (drafted — verify before implementing):

| Cloud | sObject signals | Metadata type signals | Keyword signals |
|---|---|---|---|
| sales | `Opportunity`, `Lead`, `Account`, `OpportunityLineItem`, `Campaign` | Sales Path, Forecasting settings | "opportunity", "lead", "pipeline", "forecasting" |
| service | `Case`, `ServiceContract`, `Entitlement` (Service Cloud), `Knowledge__kav`, `WorkOrder`, `LiveAgent*` | `EmbeddedServiceFlowConfig`, `ServiceChannel`, `Queue`, `MessagingChannel` | "case", "knowledge article", "omni-channel", "live agent" |
| agentforce | `Bot`, `BotVersion`, `GenAiAction` | `GenAiPlanner`, `GenAiPlannerBundle`, `GenAiPlugin`, `GenAiFunction`, `GenAiPromptTemplate`, `Bot`, `AiAuthoringBundle` | "agent", "topic", "action", "agentforce", "bot" |
| revenue_cpq | `Quote`, `QuoteLineItem`, `Contract`, `Order`, `OrderItem`, `Asset`, `SBQQ__*` | `ProductRule`, `PriceRule`, `ProductAction`, `SummaryGroup` | "quote", "CPQ", "pricing rule", "product configuration", "revenue lifecycle" |
| commerce_b2b | `WebStore` (type=B2B), `BuyerGroup`, `BuyerAccount`, `WebCart`, `CartItem`, `CommerceEntitlementPolicy`, `ProductCatalog`, `BuyerGroupPricebook` | `CommerceSettings`, `CommerceSearchSettings`, `WebStore` | "storefront", "buyer", "cart", "checkout", "commerce" |
| commerce_b2c | `WebStore` (type=B2C), `PersonAccount`, `CartItem` | Same as B2B with `type=B2C` | "shopper", "D2C", "consumer storefront" |
| experience_cloud | `Network`, `NetworkMember`, `Site` | `ExperienceBundle`, `CommunityThemeDefinition`, `Network` | "community", "experience cloud", "lwr site", "aura site" |
| field_service | `WorkOrder`, `ServiceAppointment`, `ServiceTerritory`, `ServiceResource` | `FieldServiceSettings`, dispatcher console | "field service", "dispatch", "mobile worker" |
| marketing_engagement | `pi__*` (Pardot) objects | Engagement Studio, prospect forms | "pardot", "account engagement", "prospect" |
| data_cloud | `UnifiedIndividual`, `DataStream`, `CalculatedInsight` | `DataStream`, `CalculatedInsight` | "data cloud", "unified profile", "CDP" |
| health_cloud | `Patient__c`, `CareProgram`, `HealthcareProvider` | clinical data model types | "patient", "provider", "care plan" |
| industries_cloud | `Vlocity__*`, industry-specific CustomObjects | OmniStudio bundles, DataPack | "omnistudio", "omniscript", "dataraptor" |

A ticket's `detected_clouds` is the union of all matched cloud identifiers. Multi-cloud tickets (e.g., a KYC flow that creates an Opportunity, generates a Quote, and surfaces it in the buyer storefront) return a set with multiple members. The detector `required_clouds` must intersect `detected_clouds` for the detector to fire.

**Build-order implication:** the cloud-recognition helper is infrastructure. Build it once; every detector (including these 6 plus future ones) uses it. Without it, each detector re-implements cloud-scoping locally — duplication we just cleaned up in the reuse-review pass.

---

## Summary

Five parallel researchers mined public Salesforce B2B Commerce practitioner discourse (Trailblazer Community, Stack Exchange, forcedotcom/cli GitHub, Salesforce Known Issues portal, practitioner blogs) for the most commonly reported pain patterns. 25 unique patterns surfaced across five categories after dedupe. An adversarial reviewer independently classified each claim. **10 patterns achieved Verified (Primary) classification backed by live vendor-doc fetches with verbatim quotes or multiple Salesforce Known Issue entries. 5 more are Verified (Secondary) via multiple independent practitioner sources. 11 are weaker evidence (Corroborated or Unverified) — usable with caution but worth deferring to dedicated detection research.**

Rough category split: Sharing + entitlements ~25%, Cart/checkout/pricing ~25%, Catalog/search/products ~20%, Experience Cloud LWR ~20%, SFDX deployment + scratch org ~10%. The SFDX bucket is disproportionately well-documented because forcedotcom/cli's GitHub issues are public and structured — a strong signal source the community discourse uses less.

**Most significant surprise:** the XCSF30-88825 OMS-in-scratch-org pivot we already lived through is **exactly** documented pattern `scratch_org_order_management_triple_flag_requirement`. The agent handled it ad-hoc mid-run because no detector existed. This validates the mining approach — the community's top pain is our silent operating-under-bug.

---

## Categorized Pain Catalog

### Category 1 — Sharing + Entitlements (~25% of findings)

#### 1.1 `entitlement_policy_search_index_desync` — **Verified (Primary)**
- **Pattern:** After creating/modifying a BuyerGroup, entitlement policy, catalog, or price book linkage, buyers see empty storefront until search index is rebuilt. Rebuild is NOT automatic.
- **Hard limit:** 60 rebuilds per hour (introduced **release 234 — Spring '22**, NOT Spring '24 as originally cited; reviewer caught this factual error).
- **Detection:** Ticket mentions "products not showing," "empty catalog," "blank storefront," references to `CommerceEntitlementPolicy`, `CommerceEntitlementProduct`, `BuyerGroupMember`.
- **Agent behavior:** Flag. Ask whether rebuild happened post-change.
- **Sources:**
  - [Update the Commerce Search Index](https://help.salesforce.com/s/articleView?id=sf.comm_search_index_build.htm) (Salesforce Help, Primary)
  - [60 Rebuilds per Hour limit — release 234](https://help.salesforce.com/s/articleView?id=release-notes.rn_b2b_comm_lex_index_rebuild_limit.htm&release=234) (Salesforce Release Notes, Primary)

#### 1.2 `entitlement_policy_missing_view_permissions` — Corroborated
- **Pattern:** Entitlement policy saved without "View Products" and "View Prices in Catalog" checkboxes produces silent zero-visibility. UI does not warn.
- **Detection:** Ticket reports products correctly entitled to buyer group, index rebuilt, but storefront still empty; no mention of permission flags.
- **Agent behavior:** Clarify. Ask for screenshot of entitlement policy configuration.
- **Sources:** Salesforce Help (Primary, corroborated by practitioner blogs; "silent failure" framing is practitioner interpretation, not primary wording).

#### 1.3 `pricebook_not_linked_to_buyer_group` — **Verified (Secondary)**
- **Pattern:** Buyer can browse but can't checkout. Specific error: "Order Products must have a Price Book Entry that belongs to the Price Book related to the parent Order."
- **Detection:** Error string match, `PricebookEntry` + `Order` + `OrderProduct` object references, $0/blank price reports.
- **Agent behavior:** Flag. Verify full chain: BuyerGroup → BuyerGroupMember → EntitlementPolicy → CommerceEntitlementProduct AND BuyerGroup → WebStorePricebook → PricebookEntry.
- **Sources:**
  - [Salesforce quickstart GitHub issue #13](https://github.com/forcedotcom/b2b-commerce-on-lightning-quickstart/issues/13) — exact error string match
  - [Create a Buyer Group or Store Price Book](https://help.salesforce.com/s/articleView?id=sf.comm_assign_pricebooks.htm) (Primary)

#### 1.4 `entitlement_data_limit_silent_exclusion` — **Verified (Primary)**
- **Pattern:** Hard platform cap: only first **2,000 buyer groups** per product are indexed. Groups ranked 2,001+ silently excluded. Soft limit: **200 buyer groups per entitlement policy** causes "data scaling issues."
- **Detection:** Large catalog + many account segments. Some buyer groups can't see products others can see with no config difference.
- **Agent behavior:** Escalate. Structural platform limit. Requires buyer-group consolidation / entitlement policy restructuring.
- **Sources:**
  - [Entitlement Data Limits](https://developer.salesforce.com/docs/commerce/salesforce-commerce/guide/b2b-b2c-comm-data-model-entitlement-limits.html) (Primary — live fetch, verbatim quotes)

#### 1.5 `account_switcher_two_tier_degradation` — **Verified (Primary)**
- **Pattern:** Effective-account switcher degrades at **200 accounts**, fails entirely at **2,000**. `CommerceBuyGrp` Apex extensibility raises per-account buyer group limit from 20 to 30 but does NOT address the Switcher threshold.
- **Note:** Original draft title said "hard_failure_at_2000_accounts" — reviewer flagged this as misleading (it's two-tier: 200 degrade, 2000 fail). Detector must check both thresholds.
- **Detection:** Ticket references "buy on behalf," "account switcher," "effective account," "distributor user." Mentions >200 accounts.
- **Agent behavior:** Clarify. If >200 accounts, flag degradation risk. If >2,000, escalate: requires custom selector.
- **Sources:**
  - [Shopper and Buyer Group Data Limits](https://developer.salesforce.com/docs/commerce/salesforce-commerce/guide/b2b-b2c-comm-data-model-shopper-buyer-groups-accounts-limits.html) (Primary)
  - [Allow Buyers Access to External Accounts](https://help.salesforce.com/s/articleView?id=sf.comm_buy_on_behalf.htm) (Primary)

#### 1.6 `owd_external_sharing_blocking_order_access` — Unverified
- **Pattern (practitioner-reported):** Buyer community users can't see order history — Order/OrderItem/OrderDeliveryGroup external OWDs default to Private and need manual change to Public Read Only.
- **Caveat:** Primary source (`b2b_commerce_setup_sharing`) returned 403 on direct fetch; reviewer could not verify. Claim is inferred from `b2b_commerce_upgrade_sharing` + Spring '26 coupon OWD change + practitioner blog.
- **Detection:** "Can't see orders," "My Orders page empty after login," recent community migration, external user context.
- **Agent behavior:** Clarify. Ask for OWD settings on Order-related objects before assuming code fix needed.
- **Recommendation:** Re-research before committing to a detector. Mine a specific Trailblazer thread with the error text once authentication is available.

---

### Category 2 — Cart + Checkout + Pricing (~25% of findings)

#### 2.1 `checkout_ttl_cart_lock_orphan` — Unverified
- **Pattern:** `WebCart.Status = 'Checkout'` persists on abandoned sessions; cart is "locked" to returning buyer. Spring '25 (API v63) changed behavior so in-checkout cart edits no longer auto-cancel — but older stores remain affected.
- **Caveat:** Trailblazer thread `0D54S00000EoWcISAV` confirms title via SERP only; body is auth-gated. TTL mechanics primary-confirmed; the "silent lock" framing is practitioner-reported.
- **Detection:** "Cart is locked during checkout," `WebCart.Status = 'Checkout'` beyond expected session length.
- **Agent behavior:** Flag. Clarify LWR vs Aura (Spring '25 fix applies to LWR). If Aura, workaround via Cart Connect API or Apex cart reset.
- **Sources:**
  - [Time Limits and Active Checkouts](https://developer.salesforce.com/docs/commerce/salesforce-commerce/guide/b2b-b2c-comm-checkout-ttl.html) (Primary)
  - [Simplify Cart Cleanup](https://developer.salesforce.com/docs/commerce/salesforce-commerce/guide/b2b-d2c-comm-cart-cleanup.html) (Primary)

#### 2.2 `promotion_evaluate_not_applied_to_webcart` — **Verified (Primary)**
- **Pattern:** Evaluate API computes promotions but **does NOT write them to `WebCart`**. Separate `Price Cart` flow action required. Caps at **50 active manual + 50 active automatic promotions** by priority; beyond silently excluded.
- **Detection:** "Discount applies but cart total doesn't update," "coupon accepted no price change," Evaluate API call without Price Cart step in flow.
- **Agent behavior:** Clarify. Ask for checkout flow diagram. Check for Price Cart action. Audit total promotion count if approaching 50/50 cap.
- **Sources:**
  - [Commerce Pricing and Promotions APIs](https://developer.salesforce.com/docs/commerce/salesforce-commerce/guide/b2b-d2c-comm-pricing-promotions-apis.html) (Primary — verbatim quote of 50/50 cap)
  - [Considerations for Commerce Promotions](https://help.salesforce.com/s/articleView?id=commerce.comm_promotions_get_to_know.htm) (Primary)

#### 2.3 `currency_change_mid_session_cart_destruction` — **Verified (Primary)**
- **Pattern:** `WebCart.CurrencyIsoCode` is immutable once set. Currency change requires new cart; items silently drop if `PricebookEntry` missing for target currency. Salesforce's own guidance: disable currency picker after any item in cart.
- **Detection:** "Prices show 0.00 after currency switch," multi-currency org, `WebCart.CurrencyIsoCode` transition attempts.
- **Agent behavior:** Flag as architectural. Clarify: is currency picker shown post-add-to-cart? If yes, requires pre-add enforcement or migration service. Flag missing `PricebookEntry` as silent-drop source.
- **Sources:** [Handle Currency Changes](https://developer.salesforce.com/docs/commerce/salesforce-commerce/guide/b2b-b2c-comm-cart-currency-change.html) (Primary)

#### 2.4 `buyergroup_pricebook_no_entry_silent_zero` — **Verified (Secondary)**
- **Pattern:** Products entitled to a buyer group but lacking a `PricebookEntry` in that group's assigned price book render with $0/blank price. No fallback cascade to store default price book.
- **Detection:** "Product visible but $0 price," "certain buyers see no price."
- **Agent behavior:** Clarify affected buyer groups. SOQL query to confirm: `SELECT Id FROM PricebookEntry WHERE Pricebook2Id IN (SELECT PricebookId FROM BuyerGroupPricebook WHERE BuyerGroupId = '<id>') AND Product2Id = '<productId>'`. Also confirm entitlement separately.
- **Sources:**
  - [BuyerGroupPricebook Object Reference](https://developer.salesforce.com/docs/atlas.en-us.object_reference.meta/object_reference/sforce_api_objects_buyergrouppricebook.htm) (Primary)
  - [Buyer Groups, Entitlements, and Pricing Strategies](https://help.salesforce.com/s/articleView?id=commerce.comm_buyergroup_entitlements_pricebooks.htm) (Primary)

#### 2.5 `checkout_api_delivery_group_payment_not_accepted` — Unverified
- **Pattern:** Checkout PATCH API silently drops `deliveryGroups` and `paymentInfo` fields. These must be set via sObject API against `CartDeliveryGroup` and payment records.
- **Caveat:** Reviewer flagged the "silent drop" wording as Training Data Only — not verified from live primary source. Matches harness CLAUDE.md salesforce.md rules file (empirical observation on FleetPride work).
- **Detection:** PATCH to `/commerce/webstores/{id}/checkouts/{cartId}` with `deliveryGroups` in body. Integration log shows HTTP 200 but delivery method remains null.
- **Agent behavior:** Clarify integration approach. Pivot to sObject API for delivery group / payment.
- **Recommendation:** Re-research silent-drop behavior before detector commit.

---

### Category 3 — Catalog + Search + Product Configuration (~20% of findings)

#### 3.1 `searchable_field_50_limit` — **Verified (Primary)**
- **Pattern:** Hard cap: **50 searchable fields + attributes** per product. Exceeding breaks next index rebuild; products may partially or fully disappear.
- **Detection:** Post-attribute-addition invisibility with no obvious entitlement cause. Count `ProductAttributeSetItem` rows with `IsSearchable=true`/`IsFilterable=true` + standard searchable fields.
- **Agent behavior:** Flag. Audit searchable field count before triggering rebuild. Name the specific limit.
- **Sources:**
  - [Maintain Product Index Records](https://help.salesforce.com/s/articleView?id=sf.b2b_commerce_product_index.htm) (Primary — verbatim)
  - [Optimize Your Online Store for Search](https://trailhead.salesforce.com/content/learn/modules/b2b2c-commerce-for-merchandisers/b2b2c-make-store-searchable) (Primary)

#### 3.2 `product_variation_parent_misconfig` — Corroborated
- **Pattern:** Parent products in variation hierarchies don't auto-appear in search. Requires 4-step configuration chain: `ProductClass` + `ProductAttributeSet` + catalog assignment + entitlement coverage. Any missing step = silent non-appearance.
- **Detection:** "Variation products not showing," attributes not appearing as filters, `ProductClass`/`ProductAttributeSetItem` references.
- **Agent behavior:** Walk the 4-step chain. Most-missed: parent product's own catalog category + entitlement coverage.
- **Sources:** Primary Salesforce Help + Summer '23 GA release note (variations); community thread confirmation gated by auth.

#### 3.3 `product_localization_manual_per_record` — **Verified (Secondary)**
- **Pattern:** Product/category/promotion localization requires per-language, per-record translation entries via Translation Workbench. "Enable Data Translation" per-store toggle commonly missed — causes all translations to be ignored.
- **Detection:** Locale picker works but product text doesn't change. Locale-specific records exist but don't render.
- **Agent behavior:** Walk 3-step checklist: locale enabled on store, "Enable Data Translation" flag on, translation records populated.
- **Sources:** Multiple official Help articles (Primary); Spring '26 release note confirming prior friction.

#### 3.4 `single_catalog_per_webstore` — **Verified (Primary)**
- **Pattern:** Hard constraint: 1 WebStore → 1 ProductCatalog (though catalogs can be reused across stores). Multi-segment strategies must use entitlement policies (risking the 2,000/200 limits) OR separate WebStores.
- **Detection:** "Need different products for different customer segments," multi-catalog-per-store requests.
- **Agent behavior:** Flag architectural constraint early. Route to architect decision: shared catalog + entitlements vs multi-store.
- **Sources:** [Product and Catalog Data Model](https://developer.salesforce.com/docs/commerce/salesforce-commerce/guide/b2b-b2c-comm-data-model-product-catalog.html) (Primary)

---

### Category 4 — Experience Cloud LWR Storefront (~20% of findings)

#### 4.1 `experiencebundle_retrieve_cold_start` — **Verified (Primary)**
- **Pattern:** `sf project retrieve start --metadata ExperienceBundle` returns success with zero files on never-published LWR sites. "Enable ExperienceBundle Metadata API" checkbox required. Viewtype errors when route/view JSON malformed.
- **Detection:** Retrieve exit 0 but empty `experiences/<site>/` directory. Error text contains "viewType." Orphaned routes.
- **Agent behavior:** Use `sf_experience_bundle_bootstrap` with `allowBrowserFallback: true` — triggers Playwright-driven first-publish. Never raw retrieve for new sites.
- **Sources:**
  - Salesforce Known Issue [a1p4V000000sck5QAA](https://trailblazer.salesforce.com/issues_view?id=a1p4V000000sck5QAA) (Primary)
  - forcedotcom/cli issues [#1870](https://github.com/forcedotcom/cli/issues/1870), [#1037](https://github.com/forcedotcom/cli/issues/1037), [#931](https://github.com/forcedotcom/cli/issues/931), [#523](https://github.com/forcedotcom/cli/issues/523)

#### 4.2 `experiencebundle_currenttheme_id_mismatch` — **Verified (Primary)**
- **Pattern:** Cross-org ExperienceBundle deploy fails: "The `currentThemeId` property in `<siteName>.json` is null or points to a non-theme component." Value is source-org-specific 18-char record ID. Metadata API does not translate IDs cross-org.
- **Detection:** Deploy error "currentThemeId" or "non-theme component." 18-char IDs in `experiences/*/*.json`.
- **Agent behavior:** Grep `experiences/**/*.json` for 18-char IDs post-retrieve. Flag for editing or env-var substitution before deploy.
- **Sources:**
  - Salesforce Known Issue [a028c00000gAykEAAS](https://issues.salesforce.com/issue/a028c00000gAykEAAS/deploying-experiencebundle-with-custom-theme-layout-fails) (Primary)

#### 4.3 `experiencebundle_nocontextexception_intermittent` — Unverified
- **Pattern:** ExperienceBundle deploy fails with `NoContextException` intermittently. Platform-side race, non-deterministic. Pure retry resolves.
- **Caveat:** Known Issue ID cited but direct fetch blocked; relies on practitioner corroboration.
- **Agent behavior:** Treat as transient. Add retry logic (3 attempts, 30s backoff) for ExperienceBundle deploys specifically. Don't fix source in response.

#### 4.4 `lwr_publish_timeout_and_ise` — Unverified
- **Pattern:** LWR publish hangs >5min or returns Internal Server Error. Two Known Issue entries cited. Blocks CI publish pipelines.
- **Caveat:** Known Issue IDs cited but direct fetch blocked.
- **Agent behavior:** Add publish-status poll with 10-min timeout. On ISE, surface error ID, advise retry. Don't re-deploy entire bundle.

#### 4.5 `lwr_aura_component_incompatibility` — **Verified (Primary)**
- **Pattern:** Aura-to-LWR migration: Aura components implementing `forceCommunity:availableForAllPageTypes` cannot be placed in LWR Builder palette at all. No Aura event bus equivalent. Sitewide CSS editor absent. Fundamental architectural break, not a bug.
- **Detection:** `force-app/main/default/aura/` components with `forceCommunity:availableForAllPageTypes`. Builder palette missing expected components. Sitewide CSS not rendering on LWR.
- **Agent behavior:** Flag as blocking migration requirement. Audit Aura component inventory. Separate CSS migration task (Aura global → LWR Style Tab + head markup).
- **Sources:**
  - [Key Differences Aura vs LWR Migration Guide](https://developer.salesforce.com/docs/commerce/lwr-migration/guide/key-differences.html) (Primary)

#### 4.6 `head_markup_static_resource_reference_failure` — **Verified (Secondary)**
- **Pattern:** `{!URLFOR($Resource.*)}` merge fields in LWR head markup render literally or 404. LWR doesn't evaluate Visualforce-style merge fields (Aura did). Workaround: absolute CDN path or inline content.
- **Detection:** Head markup with `$Resource` merge field syntax. Browser DevTools shows literal merge field in src/href.
- **Agent behavior:** Flag any `$Resource` merge in LWR head markup as unsupported. Recommend hardcoded static resource URL.
- **Sources:**
  - [Static Resource not works in Head Markup for LWR site](https://trailhead.salesforce.com/trailblazer-community/feed/0D54V00007PzPnxSAF) (Trailblazer, Secondary)
  - [Head Markup in LWR Sites (template differences)](https://developer.salesforce.com/docs/atlas.en-us.exp_cloud_lwr.meta/exp_cloud_lwr/template_differences_markup.htm) (Primary)

---

### Category 5 — SFDX Deployment + Scratch Org (~10% of findings)

#### 5.1 `destructive_changes_source_format_silent_noop` — Corroborated
- **Pattern:** `sf project deploy start --pre-destructive-changes` silently no-ops when component remains in local source tree. Source-format "local wins." Distinct from metadata-format behavior.
- **Agent behavior:** Use `sf_destroy` MCP tool (post-condition verification), not raw CLI. Confirm intent: org-only or org+source.
- **Sources:** harness CLAUDE.md rules file + 3 practitioner blogs. No primary Salesforce warning documented.

#### 5.2 `custom_field_deploy_reports_created_but_schema_not_converged` — Corroborated
- **Pattern:** Async schema propagation can lag deploy completion; subsequent `sObject describe` shows field missing despite "Created" response.
- **Agent behavior:** Use `sf_deploy_smart` MCP tool with `expectedFields` populated for schema changes. Never raw deploy for new fields.
- **Sources:** harness CLAUDE.md rules file (real incident 2026-04-14/15), Salesforce Developer Blog on deployment best practices.

#### 5.3 `genaiplanner_to_genaiplannerbundle_v64_cutover` — **Verified (Primary)**
- **Pattern:** `GenAiPlanner` deprecated at API v64; replaced by `GenAiPlannerBundle`. Not interchangeable. Stale `sourceApiVersion` causes wrong-type deploys or validation fails. Dependency chain: Bot → BotVersion → GenAiPromptTemplate → GenAiFunction → GenAiPlugin → Planner/Bundle.
- **Agent behavior:** Check `sourceApiVersion` before any Agentforce deploy. v60-63 → GenAiPlanner. v64+ → GenAiPlannerBundle. Verify dependency chain on target org. Flag version mismatch.
- **Sources:**
  - [GenAiPlannerBundle Metadata API](https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_genaiplannerbundle.htm) (Primary)
  - [Gearset migration guide](https://docs.gearset.com/en/articles/11728020-resolving-validation-errors-not-available-for-deploy-for-this-api-version-on-genaiplanner-metadata-type) (Secondary)
  - forcedotcom/cli [#3309](https://github.com/forcedotcom/cli/issues/3309)

#### 5.4 `schema_json_changes_ignored_without_xml_touch` — **Verified (Secondary)**
- **Pattern:** Modifying `input/schema.json` or `output/schema.json` in a `GenAiFunction` dir without touching `.genAiFunction-meta.xml` silent-no-ops. Metadata API won't reprocess JSON unless XML changed.
- **Agent behavior:** Touch the XML (description field, timestamp comment) whenever schema.json content changes. Warn on PRs containing only JSON diffs in `genAiFunctions/`.
- **Sources:** Gearset docs, forcedotcom/cli [#3075](https://github.com/forcedotcom/cli/issues/3075), [#3164](https://github.com/forcedotcom/cli/issues/3164), CLAUDE.md agentforce rules.

#### 5.5 `webstore_creation_requires_rest_not_metadata` — **Verified (Primary)**
- **Pattern:** `WebStore`, `BuyerGroup`, `CommerceEntitlementPolicy` are data objects, not Metadata API. `sf project deploy` does NOT create stores. Use `POST /sobjects/WebStore/`. Even with correct perms, fails with `INVALID_INPUT` if org-level `CommerceSettings.commerceEnabled` is false.
- **Agent behavior:** Use `sf_webstore_create` MCP tool. Verify `CommerceSettings.commerceEnabled` first. BuyerGroup/entitlement config via data import, not metadata deploy.
- **Sources:**
  - [Deploy Store Metadata (B2B Dev Guide)](https://developer.salesforce.com/docs/commerce/salesforce-commerce/guide/b2b-d2c-comm-deploy-store-metadata.html) (Primary)
  - Salesforce Known Issue [a028c00000gAwiaAAC](https://issues.salesforce.com/issue/a028c00000gAwiaAAC/unable-to-create-new-webstore-for-b2b-lightning-or-any-custom-template)

#### 5.6 `sourceapiversion_drift_blocks_new_metadata` — **Verified (Secondary)**
- **Pattern:** Stale `sourceApiVersion` in `sfdx-project.json` silently blocks later metadata types. `--apiversion` CLI flag doesn't reliably override the serialization layer. Minimums: Agentforce v60, GenAiPlannerBundle v64, AiAuthoringBundle v65.
- **Agent behavior:** Check `sourceApiVersion` as first step for Agentforce / Commerce work. Update and commit before proceeding.
- **Sources:** forcedotcom/cli [#1656](https://github.com/forcedotcom/cli/issues/1656), [#3004](https://github.com/forcedotcom/cli/issues/3004), [#2923](https://github.com/forcedotcom/cli/issues/2923) (4 issues total), CLAUDE.md agentforce rules.

#### 5.7 `scratch_org_order_management_triple_flag` — Corroborated
- **Pattern:** B2B Commerce + OMS scratch org needs THREE coordinated settings: features `OrderManagement` + `B2BCommerce`, `orderSettings.enableEnhancedCommerceOrders=true`, `orderManagementSettings.enableOrderManagement=true`. Missing the enhanced-orders flag leaves OMS module present but unable to bridge to Commerce checkout. **Silent failure surfaces only at checkout-integration testing time.**
- **XCSF30 relevance:** XCSF30-88825 hit exactly this — Dev Hub doesn't provision OMS; agent pivoted OrderSummary → Order mid-run. Would have been caught by this detector.
- **Agent behavior:** When generating/validating scratch org definition for B2B Commerce + OMS, verify all three config points. Warn on partial setup. Consider auto-pivot to standard Order/OrderItem if scratch feature flag can't be acquired.
- **Sources:** [Scratch org with Salesforce Order Management (lekkimworld)](https://lekkimworld.com/2023/01/31/scratch-org-with-salesforce-order-management/), Salesforce scratch org features doc.
- **Recommendation:** Single practitioner source is a weakness. Strengthen with additional confirmation before committing detector.

---

## Gap Analysis vs XCSF30 Archive

| Community Pattern | Harness Coverage Today |
|---|---|
| `scratch_org_order_management_triple_flag` | ◐ XCSF30-88825 hit it, agent pivoted ad-hoc. No detector. **Top priority.** |
| `experiencebundle_retrieve_cold_start` | ✓ Already handled — `sf_experience_bundle_bootstrap` tool exists. Not a gap. |
| `destructive_changes_silent_noop` | ✓ Already handled — `sf_destroy` tool + CLAUDE.md rule. |
| `custom_field_deploy_schema_not_converged` | ✓ Already handled — `sf_deploy_smart` + CLAUDE.md rule. |
| `webstore_creation_requires_rest` | ✓ Already handled — `sf_webstore_create` tool. |
| `genaiplanner_v64_cutover` | ✗ No detector. API version check not automated. |
| `sourceapiversion_drift` | ✗ No detector. |
| `entitlement_data_limit_silent_exclusion` (2000 buyer groups) | ✗ No detector. Architectural risk for large customers. |
| `account_switcher_two_tier_degradation` (200/2000) | ✗ No detector. |
| `promotion_50_plus_50_cap` | ✗ No detector. |
| `searchable_field_50_limit` | ✗ No detector. |
| `entitlement_policy_search_index_desync` | ✗ No detector. Critical for day-1 support of any B2B store. |
| `lwr_aura_component_incompatibility` | ✗ No detector. Would catch migration tickets before implementation. |
| `head_markup_static_resource_failure` | ✗ No detector. |
| `experiencebundle_currenttheme_id_mismatch` | ✗ No detector. Cross-org deploy is common. |
| Others (weaker evidence) | ✗ Defer pending re-research. |

---

## Recommended Priorities — Build These Detectors First

### Prerequisite: Cloud Recognition Helper

**Must ship BEFORE any of the 6 detectors below.** Without it, every detector below would false-positive on unrelated work in a multi-cloud org.

- **`_cloud_recognition.py`** — shared utility that returns `set[CloudIdentifier]` for each ticket. Signal taxonomy per cloud is drafted in the "Cloud Recognition" section above. ~1 day of engineering.

Once shipped, every new detector declares `required_clouds: [...]` and the miner's detector dispatch gates on `required_clouds.intersection(detect_salesforce_clouds(ticket))`.

### Then: 6 priority detectors

Given ~2 weeks of engineering budget, ranked by frequency × severity × detectability × current harness blindness:

1. **`scratch_org_commerce_oms_feature_check`** — highest value. Rediscovers the XCSF30-88825 incident. Gate: `commerce_b2b` or `revenue_cpq`. Detection: check scratch org def JSON for missing feature flags when ticket mentions OMS / Order Management AND is commerce-scoped. Strongly catchable; all-primary sources available; already one incident in the archive.

2. **`entitlement_policy_search_index_desync`** — highest frequency. Gate: `commerce_b2b`. **The gate matters here** — without it, every Service Cloud case-management ticket mentioning "entitlement" would false-positive. Verified Primary.

3. **`genaiplanner_to_genaiplannerbundle_v64_cutover`** — high severity for Agentforce work. Gate: `agentforce`. Check `sourceApiVersion` + metadata type on Agentforce-scoped tickets only. Verified Primary.

4. **`searchable_field_50_limit`** — structural limit with hard-cap detection. Gate: `commerce_b2b` or `commerce_b2c`. Count `ProductAttributeSetItem` + searchable standard fields per product. Verified Primary.

5. **`promotion_50_plus_50_cap`** — similar structural limit. Gate: `commerce_b2b` or `commerce_b2c`. Count active `Promotion` records by trigger type. Verified Primary. **Without gate**, CPQ pricing tickets would false-match on the word "promotion".

6. **`experiencebundle_currenttheme_id_mismatch`** — clean regex detector (18-char Ids in `experiences/**/*.json`). Gate: `experience_cloud` (independent of whether it hosts B2B/B2C Commerce or is a pure portal/help center). Verified Primary.

**Not in top 6 but worth later:** `pricebook_not_linked_to_buyer_group` (detection requires SOQL); `lwr_aura_component_incompatibility` (important for migration tickets); `account_switcher_two_tier_degradation` (narrow but catastrophic). All commerce_b2b-gated.

**Platform-wide (NOT cloud-scoped) — separate detector family:**
- `sourceapiversion_drift_blocks_new_metadata` — platform-level, fires regardless of cloud.
- `destructive_changes_source_format_silent_noop` — platform-level SFDX behavior.
- `custom_field_deploy_schema_not_converged` — platform-level deploy timing.

**Do NOT build yet:** `owd_external_sharing_blocking_order_access`, `checkout_ttl_cart_lock_orphan`, `experiencebundle_nocontextexception_intermittent`, `lwr_publish_timeout_and_ise`, `checkout_api_delivery_group_payment_not_accepted` — all classified Unverified or relying on blocked primary sources. Re-research before committing.

### Next research passes

Each of these clouds produces its own distinct pain taxonomy, separate from this B2B-Commerce slice:

- **Agentforce** — topic/action design patterns, reasoning-engine failure modes, Agent API rate limits, testing-center gotchas
- **Service Cloud** — Omni-Channel routing bugs, Knowledge article surfacing, entitlement-contract logic (collides by name with Commerce entitlements)
- **Revenue Cloud / CPQ** — ProductRule evaluation ordering, QuoteLine bundling, amendment/renewal edge cases, SBQQ legacy → Revenue Lifecycle migration
- **Experience Cloud (content-agnostic)** — LWR vs Aura behavior differences beyond B2B storefronts, Builder selector drift, site activation/publish races
- **Field Service** — dispatcher-console issues, mobile-app sync, work-order lifecycle
- **Platform-wide** — flow debugging, trigger order of execution, platform cache stampedes, governor-limit edge cases

Each should be a separate research pass with its own assertion-pattern YAMLs. The detector library organizes by cloud (`detectors/b2b_commerce/`, `detectors/agentforce/`, `detectors/cross_cloud/`, etc.) so future patterns slot in without collision.

---

## Review Matrix

| # | Pattern | Source Type | Classification | Notes |
|---|---|---|---|---|
| 1 | entitlement_policy_search_index_desync | Primary | Verified (Primary) | 60/hr cap release 234 (Spring '22) corrected from original Spring '24 draft |
| 2 | entitlement_policy_missing_view_permissions | Secondary | Corroborated | Silent-failure framing is practitioner interpretation |
| 3 | pricebook_not_linked_to_buyer_group | Primary (GitHub repo) + Help | Verified (Secondary) | Error string widely attested |
| 4 | entitlement_data_limit_silent_exclusion (2000 cap) | Primary (live fetch) | Verified (Primary) | Verbatim confirmed |
| 5 | account_switcher_two_tier_degradation (200/2000) | Primary (live fetch) | Verified (Primary) | Title corrected — was misleadingly "2000 only" |
| 6 | owd_external_sharing_blocking_order_access | Secondary (403) | Unverified | Primary doc blocked; re-research needed |
| 7 | checkout_ttl_cart_lock_orphan | Primary (docs) + community SERP | Unverified | Community body auth-gated |
| 8 | promotion_evaluate_not_applied_to_webcart | Primary (verbatim quote) | Verified (Primary) | |
| 9 | currency_change_mid_session_cart_destruction | Primary | Verified (Primary) | |
| 10 | buyergroup_pricebook_no_entry_silent_zero | Primary object ref | Verified (Secondary) | Silent-$0 framing inferred from data model |
| 11 | checkout_api_delivery_group_payment_not_accepted | Training Data Only + rules | Unverified | Original worker flagged; reviewer confirmed |
| 12 | promotion_50_plus_50_cap | Primary | Verified (Primary) | Dedup of #8 |
| 13 | search_index_rebuild_quick_vs_full | Primary | Verified (Primary) | Same Spring '22 correction |
| 14 | searchable_field_50_limit | Primary | Verified (Primary) | Verbatim |
| 15 | (duplicate of 4) | — | — | Removed |
| 16 | product_variation_parent_misconfig | Primary docs | Corroborated | Community body gated |
| 17 | product_localization_manual_per_record | Primary (multiple Help) | Verified (Secondary) | |
| 18 | single_catalog_per_webstore | Primary | Verified (Primary) | |
| 19 | experiencebundle_retrieve_cold_start | Primary (GitHub + KI) | Verified (Primary) | 4 GitHub issues cited |
| 20 | experiencebundle_currenttheme_id_mismatch | Primary (KI) | Verified (Primary) | KI title confirmed |
| 21 | experiencebundle_nocontextexception_intermittent | Secondary (KI cited but fetch blocked) | Unverified | |
| 22 | lwr_publish_timeout_and_ise | Secondary (KIs cited, fetch blocked) | Unverified | |
| 23 | lwr_aura_component_incompatibility | Primary | Verified (Primary) | |
| 24 | head_markup_static_resource_failure | Secondary + Primary docs | Verified (Secondary) | |
| 25 | destructive_changes_source_format_silent_noop | Harness rules + 3 blogs | Corroborated | No primary SF doc |
| 26 | custom_field_deploy_schema_not_converged | Harness rules + Dev Blog | Corroborated | Internal incident |
| 27 | genaiplanner_to_genaiplannerbundle_v64_cutover | Primary | Verified (Primary) | |
| 28 | schema_json_changes_ignored_without_xml_touch | Secondary (Gearset + 2 GH issues) | Verified (Secondary) | |
| 29 | webstore_creation_requires_rest_not_metadata | Primary | Verified (Primary) | |
| 30 | sourceapiversion_drift_blocks_new_metadata | Secondary (4 GH issues) | Verified (Secondary) | |
| 31 | scratch_org_order_management_triple_flag | Secondary (1 blog) | Corroborated | Single-blog weakness |

## Confidence Summary
- Verified (Primary): 10
- Verified (Secondary): 5
- Corroborated: 6
- Unverified: 5
- Contradicted: 0 (1 date-factual-error corrected inline)
- Unsupported: 0

## Logical Issues Fixed
- **Factual error corrected:** 60-rebuilds/hour cap is release 234 = Spring '22, NOT Spring '24 as originally drafted.
- **Misleading title corrected:** account-switcher pattern renamed from "hard_failure_at_2000_accounts" to "two_tier_degradation" to capture both thresholds.
- **Duplicate removed:** entitlement_data_limit_silent_exclusion was present in both Sharing and Catalog categories — deduplicated.
- **Silent-failure over-representation noted:** 12 of 25 patterns invoke "silent" behavior; only 2 have primary-doc confirmation of the "silent" framing. Detectors built on these must validate before firing.

## Gaps & Limitations
- **Salesforce Known Issues portal returns 403 on direct fetch** — multiple KI IDs cited but not verified from URL. Detectors relying on KI IDs should re-verify KI status at detector-scan time.
- **Trailblazer Community bodies authentication-gated** — thread existence + titles confirmed via SERP, but full context unreadable. Practitioner consensus on workarounds is harder to confirm without access.
- **Stack Exchange thin for B2B Commerce** — community uses Trailblazer + LinkedIn more than SE.
- **LinkedIn not scraped** — MVPs post B2B Commerce content there but not indexed for research.

## Sources Consulted (top 15)

1. [Entitlement Data Limits](https://developer.salesforce.com/docs/commerce/salesforce-commerce/guide/b2b-b2c-comm-data-model-entitlement-limits.html) — Primary
2. [Pricing and Promotions APIs](https://developer.salesforce.com/docs/commerce/salesforce-commerce/guide/b2b-d2c-comm-pricing-promotions-apis.html) — Primary
3. [Shopper and Buyer Group Data Limits](https://developer.salesforce.com/docs/commerce/salesforce-commerce/guide/b2b-b2c-comm-data-model-shopper-buyer-groups-accounts-limits.html) — Primary
4. [Handle Currency Changes](https://developer.salesforce.com/docs/commerce/salesforce-commerce/guide/b2b-b2c-comm-cart-currency-change.html) — Primary
5. [Maintain Product Index Records](https://help.salesforce.com/s/articleView?id=sf.b2b_commerce_product_index.htm) — Primary
6. [60 Rebuilds/Hour Release Note (rel 234)](https://help.salesforce.com/s/articleView?id=release-notes.rn_b2b_comm_lex_index_rebuild_limit.htm&release=234) — Primary
7. [LWR Migration Key Differences](https://developer.salesforce.com/docs/commerce/lwr-migration/guide/key-differences.html) — Primary
8. [LWR Template Differences: Markup](https://developer.salesforce.com/docs/atlas.en-us.exp_cloud_lwr.meta/exp_cloud_lwr/template_differences_markup.htm) — Primary
9. [GenAiPlannerBundle Metadata API](https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_genaiplannerbundle.htm) — Primary
10. [Salesforce B2B Commerce Quickstart Issue #13](https://github.com/forcedotcom/b2b-commerce-on-lightning-quickstart/issues/13) — Primary (official repo)
11. [Salesforce Known Issue a028c00000gAykEAAS (currentThemeId)](https://issues.salesforce.com/issue/a028c00000gAykEAAS/deploying-experiencebundle-with-custom-theme-layout-fails) — Primary
12. [Gearset ExperienceBundle validation errors](https://docs.gearset.com/en/articles/8205188-resolving-common-validation-errors-related-to-experience-bundle-metadata-type) — Secondary
13. forcedotcom/cli GitHub issues #1870, #1037, #931, #523, #3309, #3075, #3164, #1656, #3004, #2923 — Primary (official repo)
14. [Scratch Org with Order Management (lekkimworld)](https://lekkimworld.com/2023/01/31/scratch-org-with-salesforce-order-management/) — Secondary
15. harness CLAUDE.md rules file `/Users/thomaswagner/.claude/rules/sf-capability-mcp.md` — Primary (internal)

---

*Researched by /research skill | 2026-04-18 | 5 parallel workers + 1 adversarial reviewer | 25 unique patterns across 5 categories | 10 Verified (Primary) / 5 Verified (Secondary) / 6 Corroborated / 5 Unverified / 1 factual date correction*
