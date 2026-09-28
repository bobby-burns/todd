# iOS and Android apps in Todd: feasibility and plan

*Status: phase 1 is implemented (building and shipping). Phases 2 and 3 (an Android emulator and an iOS simulator
Todd can use) need a few decisions from you first; see [Decisions](#decisions-needed-from-you). Facts checked on
2026-09-28 against Expo, Apple and Google docs and the eas-cli source (v24.8.0).*

## Short answer

**Yes, it's possible, with one hard limit.**

| | Build | Sign and ship to the store | Run it for testing |
|---|---|---|---|
| **Android** | Yes, on Expo's servers (or locally on Linux) | Yes (Play API) | Yes: an emulator in Todd, if the host has KVM (phase 2) |
| **iOS** | Yes, on Expo's Macs; no Mac needed | Yes (App Store Connect API, TestFlight) | Not inside Todd's Linux containers. Needs a Mac, a cloud simulator or your iPhone (phase 3) |

Why iOS can't simulate inside Todd: the iOS Simulator is part of Xcode, which only runs on macOS. Apple's licence only
allows macOS virtual machines on Apple hardware you own. The Docker-OSX images were taken down after Apple's DMCA
notice (2024), and macOS 27 runs only on Apple silicon, so x86 "macOS in Docker" is a dead end even ignoring the
licence. The iOS options below all run the simulator on a real Mac: yours, a cloud provider's, or Expo's.

## The Apple Developer sign-in problem (fixed)

On the Accounts page, **All services** only *adds* a service to your list; the **Sign in** button was on the other tab.
Linked accounts (App Store Connect signs in with Apple Developer; Play Console, Firebase and Gmail sign in with
Google) had no Sign in button at all. The Setup wizard also dropped them from its sign-in queue, so picking only App
Store Connect said "Everything you picked is already signed in" when it wasn't.

Now:

- Adding a service that isn't signed in opens its sign-in right away. The login page opens in the agents' browser and
  Todd moves on when it detects the sign-in.
- Linked accounts have a Sign in button that opens the parent's login (App Store Connect → Apple Developer).
- The wizard and "Sign in to N missing" include linked accounts.

Apple's sign-in asks for a 6-digit code on one of your Apple devices the first time. Type it into the live browser.
Apple then trusts that browser profile, which Todd keeps.

**The browser session and the API key do different jobs.** The browser session covers what Apple only allows a person
to do: creating the app's record in App Store Connect, requesting API access, and the first signing certificate
(below). Everything repeatable (builds, signing profiles, TestFlight uploads, status checks) uses an App Store Connect
API key, so agents never drive Apple's website for routine work.

## Phase 1: build and ship (implemented)

### What agents can do now

A new `mobile` toolset (`services/api/todd/tools/mobile.py`):

- **`eas`**: Expo's EAS CLI, signed in with your Expo token per command. It creates projects, runs cloud builds for
  both platforms (iOS on Expo's macOS machines), submits to TestFlight and Google Play, and publishes over-the-air
  updates. The App Store Connect key and the Play service account are written to a private temp directory for one
  command and deleted afterwards. `submit`, `update`, `build --auto-submit`, `metadata:push`, `deploy --prod` and workflow
  runs ask you first; that's enforced in code, not in the prompt. Sign-in and interactive credential commands are blocked.
- **`app_store_connect`**: the App Store Connect REST API. Todd signs a 20-minute token for each call inside the API
  process, so the `.p8` key never reaches the sandbox or the model. Review submissions, releases, tester invites,
  public TestFlight links and replies to reviews ask you first.
- **`google_play`**: the Google Play Developer API with your service account, token exchanged inside the API process.
  Committing an edit (which publishes it) and replying to reviews ask you first.
- `find_integrations` now routes "Expo", "iOS app", "Android app", "TestFlight", "App Store Connect" and "Play Store"
  to these tools instead of the browser.

The agent's playbook (the toolset guide):

1. Build with Expo (React Native + TypeScript): one codebase for iOS, Android and web.
2. Check the app on the web first (`expo export --platform web`, deploy a preview, open it in the browser). Most
   logic and layout bugs show up there in minutes, before a 10–30 minute native build.
3. Build on EAS: installable test builds (Android APK, iOS internal), iOS simulator builds, store builds.
4. Hand you the install link or TestFlight to try on your phone, since Todd has no emulator yet.
5. Submit to the stores with your approval.

Verified here: the new tests in `services/api/tests/test_mobile.py`, the real eas-cli 24.8.0 run through Todd's sandbox
server, and
`create-expo-app --yes` plus the web export run for real. Not verified: a real build on Expo's servers, which needs
your Expo token.

### What you set up once

| Step | Where | Cost | Needed for |
|---|---|---|---|
| Expo account + access token | expo.dev → Access tokens → Settings → Integrations → Mobile apps | Free (15 iOS + 15 Android builds/month, low-priority queue); Starter $19/mo | Everything |
| Apple Developer Program | developer.apple.com/programs/enroll | $99/year | Signed iOS builds, TestFlight, App Store (not simulator builds) |
| Sign in to Apple Developer | Todd → Accounts | – | Creating app records, the first certificate |
| App Store Connect API access + team key (Admin) | App Store Connect → Users and Access → Integrations. The Account Holder requests access once; Apple approves case by case. The `.p8` downloads **once** | – | iOS signing and uploads without the browser |
| Key ID, Issuer ID, Team ID, `.p8` | Todd → Settings → Integrations → Mobile apps (the `.p8` is a file upload so it stays intact) | – | Same |
| Google Play developer account | play.google.com/console | $25 once | Android store releases |
| Play service account | Google Cloud: create it, enable the Play Developer API. Play Console → Users and permissions: invite its email | – | Uploads without the browser |

### Known gaps in phase 1

- **The first signed iOS build needs you once.** EAS never creates an Apple distribution certificate in non-interactive
  mode (checked in `SetUpDistributionCertificate.ts`). When that happens, the `eas` tool returns a `next_step` with the
  exact command to run once in a terminal on the Todd machine
  (`docker compose exec -it -w <app dir> sandbox bash -lc 'eas login && eas credentials -p ios; eas logout'`). After
  that, every build is automatic. Phase 1b removes this step.
- **New apps are created by hand, or by an agent in the browser.** Neither Apple's nor Google's API can create the app
  record. Agents do it in the signed-in browser, or ask you.
- **Google Play rules for new personal accounts:** a closed test with at least 12 testers opted in for 14 days before
  production access, plus verifying a real Android device in the Play Console app. Sources disagree on whether the
  very first upload can go through the API; the guide uses a draft release on the internal track, which is the
  supported path.
- **Builds on the free Expo plan queue behind paid ones**, and it can't incur charges. On paid plans a build costs
  $1–$4. Todd doesn't gate builds through the spend policy yet (it can't see Expo's bill); see open questions.
- **Known limit, as for CLI sign-ins:** during an `eas` command the keys exist as files in the sandbox's `/tmp`, so
  code already running in the sandbox at that moment could read them.

### Phase 1b: no human step for iOS signing

Create the distribution certificate and App Store provisioning profile through the App Store Connect API instead:

1. Generate an RSA key and a CSR in the API process.
2. `POST /v1/certificates` (`DISTRIBUTION`).
3. Build a `.p12`.
4. Register the bundle ID (`/v1/bundleIds`).
5. `POST /v1/profiles` (`IOS_APP_STORE`).
6. Keep all of it in the vault.

`eas` then writes a temporary `credentials.json` for builds whose profile uses `"credentialsSource": "local"`. That's
about a day's work. It can only be tested for real against your Apple team, so it waits until the API key exists.

## Phase 2: an Android emulator Todd can see and drive

### Design

- **A new `android` compose service** under a `mobile` profile (`docker compose --profile mobile up`). It runs the
  Android emulator headless with an x86-64 system image (API 35), with the screen shown over noVNC like the browser
  service, on a new `android_net` shared only with `api`.
- **Build the image from Google's SDK** instead of using budtmo/docker-android. Budtmo's free images stop at Android 14
  and collect telemetry (IP-based city and region); Google's container scripts are "experimental".
- **A `device` toolset** built like `browser_direct`: `device_install` (an EAS build URL or APK), `device_launch`,
  `device_screenshot` (returned to the model as an image), `device_ui` (`uiautomator dump` turned into indexed
  elements, like the browser's), `device_tap(index | x,y)`, `device_type`, `device_swipe`, `device_back/home`,
  `device_logs` (logcat filtered to the app, crashes first: the equivalent of `browser_console`) and `device_reset`.
  One run-wide lock, like the browser.
- **Maestro** in the sandbox for repeatable test flows (YAML) against the emulator. It works on Linux for Android and
  is Apache-2.0.
- **Dashboard:** a Live device panel next to the live browser, with **Take control**.

### The catch: KVM

The emulator is only usable with hardware acceleration (`/dev/kvm`).

| Todd's host | Emulator in Todd? | Alternative |
|---|---|---|
| Linux x86-64 (bare metal or a VM with nested virtualization) | **Yes** | – |
| Windows 11 (WSL2 with `nestedVirtualization`) | Usually | – |
| Mac with Docker Desktop | **No** (no KVM in Docker Desktop's VM) | Run the emulator natively on the Mac (Android Studio) and let Todd connect with `adb` over the network (`host.docker.internal:5555`); the `device` tools work the same |
| Linux ARM64 (Graviton, Apple-silicon VMs) | No official emulator build | Redroid (needs `--privileged` and the binder kernel module) or a cloud device |

Estimate: 3–5 days, including the image, the tools, the live panel and tests. A KVM machine is needed to test it for
real.

## Phase 3: an iOS simulator

| Option | How it works | Automation for agents | Cost | Fit |
|---|---|---|---|---|
| **A. Your own Mac as a bridge** | A small `todd-mac-bridge` server (token-protected, like the sandbox) runs on a Mac on your network or Tailscale. Todd sends it EAS simulator builds; it runs `xcrun simctl` (boot, install, launch, screenshot, openurl) | Taps, typing and the UI tree via [AXe](https://github.com/cameroncooke/AXe) or [idb](https://github.com/facebook/idb) (its client runs on Linux) | A Mac you can leave on | Best if you have a Mac. Also unlocks local iOS builds and Xcode |
| **B. Appetize.io** | Upload the EAS simulator build (`.tar.gz` of the `.app`, accepted as-is) through its REST API; stream it in Todd's browser | Its agent CLI (`@appetize/cli`: inspect, tap, screenshot) or JS SDK | Free tier about 30 min/month; paid from about $59/mo *(third-party figures)* | Quickest win, no hardware. Also does Android |
| **C. EAS Simulator** | `eas simulator --platform ios/android`: cloud simulators from Expo, streamed on expo.dev, "built for agents" | `agent-device` / Appium through `eas simulator:exec` | Early access (waitlist), pricing not public | Closest fit to Todd, once generally available |
| **D. Your iPhone** | TestFlight or an internal build; Todd sends you the link and asks what you see | None (you test) | Covered by the Apple membership | Works today. Always do it before a release |
| ✗ macOS in Docker | Docker-OSX | – | – | Rejected: breaks Apple's licence, images taken down, no macOS 27 on x86 |

Real-device clouds (BrowserStack, AWS Device Farm, Firebase Test Lab) fit automated test suites later rather than
agents poking at an app. Firebase Test Lab's iOS Robo crawler (beta) is a cheap smoke test: 5 runs/day free.

**Recommendation:**

1. Now: option D (already works) plus the web preview.
2. Next: **B** if you want an iOS simulator in Todd without buying anything; join the **C** waitlist in parallel and
   switch when it opens.
3. If you have a Mac: **A**. It gives the best control, needs no subscription, and later lets Todd build and debug
   natively.

Estimates: B about 2 days; A 4–6 days (bridge, tools, live view); C about 1 day once access is granted.

## Decisions needed from you

1. **What machine runs Todd?** This decides phase 2: Linux with KVM puts the emulator in Todd; a Mac means the
   emulator runs natively on it.
2. **Do you have a Mac that could stay on?** If yes, option A for iOS. If not, B or C.
3. **Is a subscription OK for a cloud simulator** (Appetize, about $59/mo), or wait for EAS Simulator?
4. **Should EAS builds count against Todd's spend policy?** For example, ask before builds when you're on a paid Expo
   plan.
5. **Is Expo the default stack?** The alternatives: Flutter (also builds iOS only on a Mac or in the cloud), or native
   Swift/Kotlin (Swift needs a Mac or macOS CI such as GitHub Actions or Codemagic).
6. **Enroll now?** Apple Developer ($99/yr) and Google Play ($25) are needed before any store release. Apple's API
   access request can take a while.

## Sources

- Expo: [builds](https://docs.expo.dev/build/introduction/), [build infrastructure](https://docs.expo.dev/build-reference/infrastructure/), [programmatic access / EXPO_TOKEN](https://docs.expo.dev/accounts/programmatic-access/), [building on CI (EXPO_ASC_* variables)](https://docs.expo.dev/build/building-on-ci/), [simulator builds](https://docs.expo.dev/build-reference/simulators/), [submit to App Store](https://docs.expo.dev/submit/ios/), [submit to Play](https://docs.expo.dev/submit/android/), [pricing](https://expo.dev/pricing), [EAS Simulator](https://expo.dev/services/simulators), [eas-cli source](https://github.com/expo/eas-cli)
- Apple: [enroll](https://developer.apple.com/programs/enroll/), [memberships](https://developer.apple.com/support/compare-memberships/), [App Store Connect API keys](https://developer.apple.com/documentation/appstoreconnectapi/creating-api-keys-for-app-store-connect-api), [two-factor authentication](https://support.apple.com/en-us/102660), [macOS licence](https://www.apple.com/legal/sla/docs/macOS27.pdf), [upload requirements](https://developer.apple.com/news/upcoming-requirements/)
- Google: [Play registration](https://support.google.com/googleplay/android-developer/answer/6112435), [testing requirements for new personal accounts](https://support.google.com/googleplay/android-developer/answer/14151465), [Play Developer API setup](https://developers.google.com/android-publisher/getting_started), [emulator command line](https://developer.android.com/studio/run/emulator-commandline), [android-emulator-container-scripts](https://github.com/google/android-emulator-container-scripts)
- Simulators and devices: [Appetize (iOS uploads)](https://docs.appetize.io/platform/app-management/uploading-apps/ios), [Appetize agent CLI](https://docs.appetize.io/agentic-flows/getting-started), [Maestro](https://github.com/mobile-dev-inc/maestro), [budtmo/docker-android](https://github.com/budtmo/docker-android), [Redroid](https://github.com/remote-android/redroid-doc), [Firebase Test Lab for iOS](https://firebase.google.com/docs/test-lab/ios/get-started), [GitHub Actions macOS images](https://github.com/actions/runner-images)
