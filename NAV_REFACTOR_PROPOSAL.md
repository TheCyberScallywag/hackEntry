# Navigation bar refactoring proposal

Written 18 November 2026 against branch `refactor/refactor-the-ui-to-be-more-compact`.
The proposal was approved on 18 November 2026 and implemented the next day. The rest of the
document is the proposal as written.

## Status

Implemented as proposed, with the five decisions answered as follows:

| Decision | Answer |
|---|---|
| 1. "Matching option" | Remove the *My matches* link; keep the page. |
| 2. Notifications | Unread badge on the avatar; link inside the account menu. |
| 3. Avatar | Initials. |
| 4. Mobile | Ionic side menu left as it is. |
| 5. Script | No JavaScript. |

The bar is in `src/app/web/templates/_nav.html`. Two details differ from the text below: the
demo script needed three steps reworded, not one, and the README's quick tour still says "header
switcher", which remains accurate enough to leave.

## Summary

The signed-in bar in `src/app/web/templates/base.html` is a flat row of up to 14 links, an
organisation switcher and a sign-out button. I suggest replacing it with four items:

```
[mark] Name    Home    Volunteer ▾    <Active organisation> ▾              (AB) ▾
```

- **Account menu** under the user's avatar: everything that identifies or belongs to the person.
- **Volunteer** dropdown: what the person does as a volunteer.
- **Organisation** dropdown, labelled with the active organisation: what they do for that
  organisation, plus switching.
- **My matches is removed from the bar.** Every remaining entry is shown only when the user can
  act on it.

The change is confined to the page frame: one template, the stylesheet, one small Jinja filter
and tests. No routes, services or queries change.

Five decisions need your answer before any work starts. They are listed at the end.

## What the bar does today

| Problem | Where |
|---|---|
| Personal, volunteer, organisation and system links are mixed in one row with no grouping. | `base.html:27-45` |
| The display name sits permanently on the bar, inside the sign-out button. | `base.html:43` |
| The organisation switcher is rendered, with a disabled button, for users who have no organisation. | `base.html:18-26` |
| Switching always lands on the new organisation's projects page, because the `next` field is sent empty. | `base.html:20`, `organisations.py:62` |
| *Suggest* is shown to accounts that are not active, although `create_submission` rejects them. | `base.html:33`, `services/submissions.py:77` |
| *My matches* and *Opportunities* are shown to users who volunteer nowhere, where both pages are empty. | `base.html:30-31` |
| Nothing marks the current page. `render` already passes `path` to every template, and nothing uses it. | `deps.py:253` |
| With `flex-wrap` and this many links, the bar wraps to several rows on a phone. | `style.css:24,29` |

## Proposed structure

### Account menu (under the avatar)

The trigger is a round avatar. A badge on it carries the unread notification count.
The panel opens with an identity header and then the personal links:

| Entry | Shown when |
|---|---|
| Display name, account status, system roles (header, not links) | always |
| Profile | always |
| Notifications, with the unread count | always |
| My suggestions | always |
| API docs | the user has a system role (as today) |
| Sign out | always |

The account has no picture today: the `users` table has no image column. I suggest an initials avatar, reusing the existing `.org-logo.initials` style, computed by
a new `initials` Jinja filter. A photo upload would add storage, moderation and more personal
data, so I have left it out.

### Volunteer dropdown

| Entry | Shown when |
|---|---|
| Opportunities | the user volunteers with at least one organisation |
| Suggest a project | the account is active and the user has an active organisation |
| Find organisations (`/orgs`) | always |
| Showcase | always |

A new account therefore sees only *Find organisations* and *Showcase*, which are the two things
it can use.

### Organisation dropdown

The whole dropdown is hidden when the user has no active organisation. Its label is the active
organisation's name, so the bar always says which organisation the links below apply to.

| Entry | Shown when |
|---|---|
| Projects | always (within the dropdown) |
| Review queue | reviewer in the active organisation |
| Coverage | reviewer in the active organisation |
| Volunteers | org admin in the active organisation |
| Switch to: one button per other organisation | the user has more than one organisation |

The switch buttons replace the `<select>` and its *Switch* button. Each is a small POST form to
the existing `/orgs/switch` route, so the CSRF check and the access check are unchanged.

### What each kind of user sees

| User | Home | Volunteer | Organisation | Account |
|---|---|---|---|---|
| New account, no organisation | yes | Find organisations, Showcase | hidden | yes |
| Volunteer in one organisation | yes | all four entries | Projects | yes |
| Volunteer in several | yes | all four entries | Projects, Switch to | yes |
| Reviewer who does not volunteer | yes | Suggest, Find organisations, Showcase | Projects, Review queue, Coverage | yes |
| Org admin | yes | as their volunteering allows | adds Volunteers | yes |
| System admin or moderator | yes | as their volunteering allows | as their roles allow | adds API docs |

## Removing *My matches* from the bar

I have read "there should not be a matching option in the menu" as: matching is something the
system does, so it should not be a menu entry. Please confirm this reading (decision 1).

The case for it: the matches page is a read-only ranked list with two filters. The page where
the volunteer acts on a match, by answering yes or no, is *Opportunities*.

I suggest removing the link only, and keeping the `/matches` route and page. It stays reachable
from three places that already link to it:

- Home, "All matches and filters" (`home.html:25`)
- each organisation card on Home (`home.html:14`)
- Showcase, "See my matches here" (`showcase.html:52`)

Keeping the route means the six test files that request `/matches` are unaffected. One document
needs rewording: `docs/content/project/demo-script.md:19` tells the presenter to open
*My matches*, which would become "Home, then All matches and filters".

## Implementation notes

- **Dropdowns without JavaScript.** The page CSP (`main.py:30`) forbids inline scripts and inline
  styles, and the web app ships no script at all. Native `<details>` elements work within that,
  and the profile page already uses them for its accordions. Giving the three menus the same
  `name` attribute makes the browser close one when another opens.
- **Known limit of `<details>`.** A menu does not close on Escape or on a click elsewhere. A
  script of about 15 lines served from `/static/` would add both, and the CSP already allows
  same-origin scripts. I suggest shipping without it first (decision 5).
- **No inline styles** means the avatar cannot take a per-user colour from a `style` attribute.
  It would use one brand colour.
- **No new queries.** Every condition above reads values `nav_for` already loads: account
  status, system roles, and each organisation's `opted_in`, `reviewer` and `org_admin` flags. The
  connection budget test (`test_web_pages.py:243`) is unaffected.
- **Current page.** Use the existing `path` value to set `aria-current="page"` on the matching
  link, and style it.
- **Switch destination.** Send `next` as the current path when that path is not under `/o/`, so
  switching from Home or Opportunities stays on that page. Organisation pages keep today's
  behaviour.
- **Error pages** render through the same frame, so they pick the new bar up with no extra work.
  The existing assertion that a 404 page contains "Sign out" still holds.

### Files touched

| File | Change |
|---|---|
| `src/app/web/templates/base.html` | Replace lines 17-46 with the new bar, probably via a new `_nav.html` partial. |
| `src/app/web/static/style.css` | About 25 lines for the menu, panel, avatar and badge. The `.switcher` rules go. |
| `src/app/web/deps.py` | Register the `initials` filter. |
| `tests/test_web_pages.py` | New tests: one per row of the table above, asserting which links are and are not rendered. |
| `docs/content/project/demo-script.md` | Reword the *My matches* step. |

## What this would improve

- **Less to scan.** A volunteer in one organisation goes from 10 links, a select, and two buttons
  to four items on the bar. A system admin goes from 14 links to the same four.
- **Fewer dead ends.** The three cases in the first table where a link leads to an empty page or
  a rejected form are removed. The disabled switcher disappears.
- **Personal details in one place.** The display name, account status and roles are shown only
  when the user opens their own menu, which matters when the screen is shared or projected
  during a demo. One block of the template renders them, so it is easy to audit.
- **Context is visible.** The active organisation's name is the label of the menu it governs.
- **Keyboard and screen-reader use.** With the menus closed there are about five tab stops, down
  from as many as 18, and the current page is announced.
- **Small screens.** Four items fit on one row at phone width.
- **Maintenance.** A new page has an obvious group to join, and the per-user tests fail if a link
  appears for someone who cannot use it.

Page speed would not change in any way a user could notice. The frame runs the same queries and
the HTML shrinks by a few hundred bytes.

## Risks

- **Matches are less discoverable.** They move from one click to two (Home, then the link).
- **One more click for most pages.** Every destination except Home is now inside a menu.
- **Mobile differs.** The Ionic app's side menu (`mobile/src/app/app.component.html`) still lists
  *My matches*, and its spec asserts that text. The two surfaces would diverge unless the mobile
  menu is changed too (decision 4).

## Decisions needed

1. **"Matching option".** Is removing the *My matches* link, while keeping the page, what you
   meant? The other candidate is *Coverage*, the reviewer's view of how well volunteers cover a
   project's skills.
2. **Notifications.** Unread badge on the avatar with the link inside the account menu
   (suggested), or a separate bell on the bar.
3. **Avatar.** Initials (suggested), or add photo upload as separate work.
4. **Mobile.** Leave the Ionic menu as it is for now (suggested), or change it in the same piece
   of work.
5. **Script.** Ship the menus with no JavaScript (suggested), or include the small script for
   Escape and click-away.
