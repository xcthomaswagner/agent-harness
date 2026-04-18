# LWR / Experience Cloud Gotchas

Load this when implementing against a Salesforce Experience Cloud site (LWR template). These are landmines discovered during live B2B Commerce storefront builds. Each item lists the symptom and the working path, so agents can skip the 20-minute dead ends.

---

## 1. Commerce wire adapters fail at deploy time

**Symptom.** Deploying an LWC that imports from `commerce/productApi`, `commerce/contextApi`, or `commerce/productCategoryApi` fails with `LWC1503: ... is not a known adapter`.

**Cause.** These wire adapters resolve only at LWR runtime; they are not registered during deploy compilation.

**Working path.** Use a custom `@AuraEnabled` Apex controller that calls `ConnectApi.CommerceSearch.searchProducts()` / `ConnectApi.CommerceCatalog.getProduct()` and returns `Map<String, Object>`. LWCs call imperatively:

```js
import searchProducts from '@salesforce/apex/FpCommerceController.searchProducts';
const results = await searchProducts({ webstoreId, categoryId });
```

**Do NOT mark these methods `cacheable=true`** when calling imperatively with `await` — cacheable methods reject imperative calls.

## 2. Commerce search requires searchTerm OR categoryId, never both empty

Empty `searchTerm=''` alone fails validation. `searchTerm='*'` returns 0 results. If `categoryId` is provided, set `searchTerm=''`; if no `categoryId`, pass a real broad term like `'truck'`.

## 3. Search index must be manually rebuilt after bulk product loads

Symptom: storefront returns 0 results even though products exist and `ProductCategoryProduct` junctions are in place.

Fix: `POST /services/data/v60.0/commerce/management/webstores/{id}/search/indexes` to kick off a rebuild. Poll `GET .../search/indexes` until complete. Prior completed indexes do not auto-refresh when the catalog changes.

## 4. productListImage ≠ tileImage (storefront PLP rendering)

The Commerce LWR Product List Page resolves `defaultImage` from ProductMedia attached to the **`productListImage`** ElectronicMediaGroup (UsageType=Listing). It does **not** use `tileImage`, despite the confusing name.

**Symptom.** Products render real images on the PDP but the `/img/b2b/default-product-image.svg` placeholder on the PLP, even after a full search-index rebuild.

**Fix.** Attach a ProductMedia for the `productListImage` group (query `ElectronicMediaGroup` by `DeveloperName`), then rebuild the search index.

**Slot map** (stock ElectronicMediaGroup records):
| DeveloperName | UsageType | Consumer |
|---|---|---|
| `productListImage` | Listing | **PLP tile image (required for cards)** |
| `productDetailImage` | Standard | PDP hero |
| `tileImage` | Tile | Not used by default LWR; custom components only |
| `bannerImage` | Banner | Category banners |
| `attachment` | Attachment | PDP downloads |

## 5. Buyer users need three permission sets

Without all three assigned, `/webruntime/api/services/data/v66.0/commerce/webstores/{id}/session-context?asGuest=false` returns 403 and the buyer appears guest-logged-in even though `LoginHistory.Status='Success'`:

- `B2BBuyer`
- `CommerceUser`
- `B2B_Commerce_User`

A custom Apex controller also needs its own permission set granting object reads on `Product2`, `PricebookEntry`, `ProductCategory`, `ProductCategoryProduct`, `ProductCatalog`, `WebStore`. (Note: `ProductCategory` read requires `ProductCatalog` read — the perm set validator rejects one without the other.)

## 6. First-party cookie setting controls session persistence

Setup → My Domain → Routing and Policies → Cookies → **Require first-party use of Salesforce cookies**. Without this, `LoginHistory.Status='Success'` but the session cookie never sticks on `.my.site.com` because modern browsers block the cross-domain `.salesforce.com` cookie.

Symptom: "We can't display this page because your browser blocks cross-domain cookies."

## 7. LWR login form field names are `un` / `pw`, not `username` / `password`

Raw POST to `/<site>/s/login` works:

```html
<form action="/fptest/s/login?ec=302&startURL=%2Ffptest%2F" method="POST">
  <input name="un" />
  <input name="pw" type="password" />
</form>
```

Returns 302 on success, 401 on bad creds. `Site.login()` Apex returns NULL when called as admin — it only works in guest-user site context.

## 8. NavigationMenuItem does not delete via source deploy

Metadata deploy is additive, not authoritative — orphaned menu items stay. Delete via REST:

```
DELETE /services/data/v60.0/sobjects/NavigationMenuItem/{id}
```

Tooling API returns NOT_FOUND for NavigationMenuItem; use standard REST. Sites have per-network menus AND `Default_User_Profile_Menu` (user icon dropdown) separately from `Default_My_Account_Menu` (left-nav on /account).

## 9. `ProductCategory.IsNavigational` hides categories from auto-nav

Set to `false` to hide a category from the header/menus without deleting it.

## 10. DigitalExperienceBundle content.json: structural IDs are load-bearing

Component IDs must match regex `^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[A-Z0-9]{28})$`. Free-form strings like `fp-header-001` are rejected.

**Detail views (`detail_01t`, `detail_0ZG`) throw "unexpected error"** when content.json is replaced via source deploy IF the outer `sldsFlexibleLayout` component ID, content region ID, or sfdcHiddenRegion ID change. Preserve the original IDs by retrieving first, then replace ONLY the inner children of the content region. Section wrappers and components get fresh UUIDs; the structural scaffold IDs stay untouched.

## 11. Mobile/tablet variant IDs must resolve against the primary content

When you replace primary content.json, clear the mobile/tablet variants (empty `children: []`) or deploy fails with "component X doesn't exist in the primary content."

## 12. Strip `geoBotsAllowed` from `sfdc_cms__site` content.json before deploy

Deploy validator rejects it as an unknown property (`additionalProperties: false`).

## 13. ThemeLayout editing is schema-strict — don't replace header/footer via source deploy

`commerce:layoutSite` header/footer regions expect specific wrapper components (`commerce_builder:layoutHeaderOne`), not bare `c:customComponent` children. Source-deploying a replacement is blocked by schema validation. Workaround: keep the OOB theme layout, put all custom content in the view's `content` region instead.

## 14. Commerce Store (LWR) wizard auto-creates its WebStore

When the New Experience Site wizard runs with the "Commerce Store (LWR)" template, it creates **both** the Network AND a WebStore (Type=B2B) and auto-binds them via `WebStoreNetwork`. Pre-creating a WebStore with the same name leaves an orphaned WebStore and blocks wizard binding with `We can't create the store site because it has a store association conflict`.

**Rule.** For Commerce Store (LWR) sites, run the site-creation flow FIRST; the wizard creates the WebStore. Only pre-create a WebStore for headless commerce or non-commerce Experience templates.

Cleanup if pre-created: `sf data delete record --sobject WebStore --record-id <orphaned-id>`.

## 15. `CommerceSettings.commerceEnabled` is the real gate for WebStore creates

WebStore describe reports `createable: true` and queries succeed, yet `POST /sobjects/WebStore/` returns `HTTP 400 INVALID_INPUT: "You don't have permission to create a store of type B2B on this org"` until org-level `CommerceSettings.commerceEnabled` is `true`.

- Not a per-user permission — org-level gate
- `CommerceAdmin` / `CommerceUser` permsets don't unlock it
- PSLs (`B2BCommerce_B2BCommerceAdminPsl`, `CommerceAdminUserPsl`) don't unlock it
- Describe does not surface the gate (`createable: true` is misleading)

Fix: deploy a minimal `Commerce.settings` metadata file flipping `commerceEnabled` to `true` (API v60+; `commerceAppEnabled` is NOT valid at v60).

## 16. Experience Builder "Publish" is required more often than the docs suggest

Required for:
- Any DigitalExperienceBundle change (view content.json, themeLayout, branding, styles.css)
- New LWC bundles (first-time deploy)
- **Changes to existing LWC source** — LWR caches the published bundle; source-deploying updated LWC JS/HTML requires republish to go live

## 17. ConnectApi return types are inconsistent

- `ConnectApi.ProductSummary.fields` returns `Map<String, ConnectApi.FieldValue>` → access via `fieldValue.value`. `String.valueOf(fieldValue)` yields garbage like `ConnectApi.FieldValue[buildVersion=60.0, value=FP-WHE-010]`.
- `ConnectApi.ProductDetail.fields` returns `Map<String, String>` directly (different API).
- `ConnectApi.ProductSummary.prices` is **null** in search results. Fetch prices separately via entitled pricing or a `PricebookEntry` query.

## 18. Wishlist / ContactPointAddress quirks

- `Wishlist.AccountId` is required on insert (not nullable).
- `WishlistItem` requires `Name` explicitly — Master-Detail via `WishlistId` does NOT auto-populate.
- `WishlistItem.Quantity` does not exist — no quantity tracking on wishlist items.
- `ContactPointAddress` is the B2B address object (not `Address` / `ContactAddress`). Attach via `ParentId`. Supports `IsDefault` (per AddressType) and `IsPrimary` (one per parent). `AddressType = 'Billing' | 'Shipping'`.

## 19. LWC static-resource paths differ by context

- In JS imports: `import LOGO from '@salesforce/resourceUrl/fptestFPLogo';`
- In LWC `<img>` tags: `<img src={LOGO} />` (bound to the import)
- In CSS `background` / external file paths: `/sfsites/c/resource/fptestFPLogo`
- For `siteLogo` component `imageInfo` attribute in themeLayout: the `/sfsites/c/resource/{name}` path directly

## 20. LWC shadow DOM: `:host` selectors for isolation

External CSS in a StaticResource does not cross shadow boundaries. Style your own components with `:host` selectors inside each LWC's `.css`. Don't try to style OOB Commerce components via a global stylesheet — it will not work.

## 21. Community layout section padding

Controlled by `community_layout-section.comm-section-container` (underscore in `community_layout-`, hyphen in `-section`). CSS variables `--dxp-c-l-section-vertical-padding`, `--dxp-c-m-*`, `--dxp-c-s-*`, `--dxp-c-section-vertical-padding` control spacing at desktop/tablet/mobile breakpoints. Zero all four to collapse.

## 22. Edge-to-edge layout inside a bounded parent

```css
width: 100vw;
margin-left: calc(50% - 50vw);
margin-right: calc(50% - 50vw);
```

Breaks out of a centered max-width container without restructuring the DOM.

---

## Related references

- `DEPLOYMENT_GOTCHAS.md` — SFDX / metadata deployment pitfalls that apply to LWR too
- `~/.claude/rules/sf-capability-mcp.md` — Prefer `sf_deploy_smart`, `sf_destroy`, `sf_experience_bundle_bootstrap` MCP tools over raw `sf` CLI for Experience Cloud work
- `reference_sf_capability_mcp_findings.md` (brain memory) — live-validated findings from DevSandbox shakedowns
