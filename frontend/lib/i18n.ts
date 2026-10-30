"use client";

/**
 * Minimal visitor-facing i18n for the public pages (/u/[token], /b/[uuid])
 * and the shared BookingDialog. Not wired to the authed app — those pages
 * stay in English for now.
 *
 * Locale is picked from, in order: `?lang=cs|en` URL param, localStorage,
 * `navigator.language` (cs* and sk* → cs, everything else → en). The pill
 * switch in the brand bar lets the visitor flip, and the choice persists
 * across tabs via the `storage` event.
 */

import { useEffect, useState } from "react";

export type Locale = "cs" | "en";

const STORAGE_KEY = "slotly:locale";

function fromBrowser(): Locale {
  if (typeof navigator === "undefined") return "en";
  const l = (navigator.language || "en").toLowerCase();
  // Slovaks typically read Czech content natively; treat sk-* as cs so the
  // interface defaults to the language they can read, not English.
  return l.startsWith("cs") || l.startsWith("sk") ? "cs" : "en";
}

function fromUrl(): Locale | null {
  if (typeof window === "undefined") return null;
  const q = new URLSearchParams(window.location.search).get("lang");
  return q === "cs" || q === "en" ? q : null;
}

function fromStorage(): Locale | null {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return v === "cs" || v === "en" ? v : null;
  } catch {
    return null;
  }
}

export function getInitialLocale(): Locale {
  return fromUrl() ?? fromStorage() ?? fromBrowser();
}

export function useLocale(): [Locale, (l: Locale) => void] {
  // Start English on the server side (and the first client render) so the
  // Next.js hydration pass matches, then flip to the real choice in an
  // effect. Visitors in Czech browsers see a one-frame English flash on
  // very slow devices — acceptable in exchange for zero hydration warnings.
  const [locale, setLocaleState] = useState<Locale>("en");

  useEffect(() => {
    // Hydration-safe locale pickup — navigator/localStorage are client-only,
    // so SSR renders English, and the real choice is applied on mount.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLocaleState(getInitialLocale());
    function onStorage(e: StorageEvent) {
      if (e.key === STORAGE_KEY && (e.newValue === "cs" || e.newValue === "en")) {
        setLocaleState(e.newValue);
      }
    }
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  function setLocale(l: Locale) {
    try {
      localStorage.setItem(STORAGE_KEY, l);
    } catch {
      /* storage disabled — in-memory only */
    }
    setLocaleState(l);
  }
  return [locale, setLocale];
}

// ---------------------------------------------------------------------------
// Dictionaries
// ---------------------------------------------------------------------------

type Dict = {
  // Shared
  loading: string;
  getYourOwn: string;
  langSwitchAria: string;

  // /u/[token] — public profile / booking landing
  profileNotFound: string;
  profileNotFoundHint: string;
  countryAndWindow: (country: string) => string;
  notAcceptingBookings: string;
  inPersonOnlyTitle: string;
  inPersonOnlyHint: string;
  bookingsOffFor: (name: string) => string;
  bookingsOffHint: string;
  noFreeTime: string;
  pickAMeeting: string;
  pickAMeetingHint: string;
  backToMeetingTypes: string;
  meetingLinkLabel: string;
  meetingLinkAuto: string;
  meetingLinkGoogleMeet: string;
  meetingLinkTeams: string;
  bookingRequestSent: (name: string) => string;
  bookingConfirmed: string;
  joinMeeting: string;
  manageBooking: string;

  // /b/[uuid] — manage booking
  loadingYourBooking: string;
  bookingNotFoundTitle: string;
  bookingNotFoundBody: string;
  couldntLoadBooking: string;
  meetingWith: (name: string) => string;
  bookingAttendeeWith: (name: string) => string;
  rowWhen: string;
  rowWhere: string;
  rowBookedAs: string;
  bookingCancelledTemplate: (when: string | null) => string;
  bookingAlreadyEnded: string;
  cancelBookingAction: string;
  rescheduleAction: string;
  cancelConfirmTitle: string;
  cancelConfirmBody: string;
  cancelReasonLabel: string;
  cancelReasonPlaceholder: string;
  keepIt: string;
  yesCancel: string;
  cancelling: string;
  couldntCancel: string;
  backToBooking: string;
  pickNewTime: string;
  rescheduleHint: (name: string) => string;
  couldntLoadAvailability: string;
  moveMeetingTitle: string;
  moveMeetingBody: (name: string) => string;
  rowWas: string;
  rowNow: string;
  backBtn: string;
  confirmMove: string;
  moving: string;
  couldntReschedule: string;
  statusCancelled: string;
  statusEnded: string;
  statusConfirmed: string;

  // BookingDialog
  dialogTitlePublicWith: (name: string) => string;
  dialogTitlePublic: string;
  dialogTitleAuthed: string;
  pickLocation: string;
  enterDifferentAddress: string;
  enterDifferentAddressSelect: string;
  yourAddress: string;
  whereLabel: string;
  addressPlaceholder: string;
  durationLabel: string;
  startLabel: string;
  noStartFits: string;
  onlyFitsHint: (duration: string) => string;
  yourName: string;
  yourEmail: string;
  titleLabel: string;
  notesLabel: string;
  notesPlaceholderOnline: string;
  notesPlaceholderPhysical: string;
  addNote: string;
  cancelAction: string;
  bookAction: string;
  sendRequestAction: string;
  bookingInProgress: string;
  sending: string;
  tileOnline: string;
  tileOnlineSub: string;
  tilePhysical: string;
  tilePhysicalSub: string;
  recommended: string;
  pickOneOption: string;
  requiredField: string;

  // SlotsCalendar
  viewDay: string;
  viewThreeDays: string;
  viewWeek: string;
  previousDay: string;
  previousThreeDays: string;
  previousWeek: string;
  nextDay: string;
  nextThreeDays: string;
  nextWeek: string;
  freeIntervalTooltip: (when: string) => string;
  todayLabel: string;
  weekLabel: (n: number) => string;
  slotTooltipBookable: (dur: number, from: string, to: string, latest: string) => string;
  slotTooltipCopyable: (dur: number, from: string, to: string, latest: string) => string;
};

export const T: Record<Locale, Dict> = {
  cs: {
    loading: "Načítám…",
    getYourOwn: "Chci také",
    langSwitchAria: "Zvolit jazyk",

    profileNotFound: "Profil nenalezen",
    profileNotFoundHint: "Tento odkaz je neplatný nebo již není veřejný.",
    countryAndWindow: (country) => `${country} · Volné termíny v příštích 8 týdnech`,
    notAcceptingBookings:
      "Tento uživatel momentálně nepřijímá rezervace — kontaktujte ho prosím přímo.",
    inPersonOnlyTitle: "Tento uživatel přijímá jen osobní schůzky.",
    inPersonOnlyHint: "Vyberte volný termín a zobrazí se dostupná místa setkání.",
    bookingsOffFor: (name) => `${name} zatím nepřijímá přímé rezervace.`,
    bookingsOffHint:
      "Dostupné termíny si můžete prohlédnout níže — pro domluvu schůzky kontaktujte druhou stranu jinou cestou.",
    noFreeTime: "V nejbližších týdnech nejsou žádné volné termíny.",
    pickAMeeting: "Vyberte druh schůzky",
    pickAMeetingHint: "V dalším kroku uvidíte, kdy máme oba volno.",
    backToMeetingTypes: "Zpět na výběr druhu schůzky",
    meetingLinkLabel: "Platforma pro online schůzku",
    meetingLinkAuto: "Automaticky",
    meetingLinkGoogleMeet: "Google Meet",
    meetingLinkTeams: "Microsoft Teams",
    bookingRequestSent: (name) =>
      `Žádost odeslána. ${name} vám odpoví potvrzením nebo odmítnutím — e-mail dostanete v obou případech.`,
    bookingConfirmed: "Hotovo! Pozvánka vám dorazí e-mailem.",
    joinMeeting: "Připojit se ke schůzce",
    manageBooking: "Spravovat rezervaci",

    loadingYourBooking: "Načítám rezervaci…",
    bookingNotFoundTitle: "Rezervace nenalezena",
    bookingNotFoundBody:
      "Tento odkaz neodpovídá žádné rezervaci. Buď jste ho zadali chybně, nebo byla rezervace mezitím z kalendáře smazána.",
    couldntLoadBooking: "Rezervaci se nepodařilo načíst. Zkuste to prosím za chvíli znovu.",
    meetingWith: (name) => `Schůzka — ${name}`,
    bookingAttendeeWith: (name) => `pořádá ${name}`,
    rowWhen: "Kdy",
    rowWhere: "Kde",
    rowBookedAs: "Rezervováno jako",
    bookingCancelledTemplate: (when) =>
      when
        ? `Tato rezervace byla zrušena (${when}). Organizátor byl informován.`
        : "Tato rezervace byla zrušena. Organizátor byl informován.",
    bookingAlreadyEnded: "Tato schůzka už proběhla.",
    cancelBookingAction: "Zrušit",
    rescheduleAction: "Přesunout",
    cancelConfirmTitle: "Zrušit tuto rezervaci?",
    cancelConfirmBody:
      "Událost z kalendáře bude odstraněna a organizátor bude informován. Tato akce se nedá vrátit zpět.",
    cancelReasonLabel: "Důvod (nepovinné)",
    cancelReasonPlaceholder: "Dejte vědět organizátorovi, co se stalo.",
    keepIt: "Ponechat",
    yesCancel: "Ano, zrušit",
    cancelling: "Ruším…",
    couldntCancel: "Nepodařilo se zrušit",
    backToBooking: "Zpět na rezervaci",
    pickNewTime: "Vyberte nový termín",
    rescheduleHint: (name) =>
      `Volné termíny — ${name}, dalších 8 týdnů. Klikněte na volné okno — původní událost zmizí a vznikne nová pozvánka.`,
    couldntLoadAvailability: "Nepodařilo se načíst volné termíny.",
    moveMeetingTitle: "Přesunout schůzku?",
    moveMeetingBody: (name) =>
      `Potvrďte nový termín — ${name} bude informován(a) a kalendářová událost se všem aktualizuje.`,
    rowWas: "Bylo",
    rowNow: "Nyní",
    backBtn: "Zpět",
    confirmMove: "Potvrdit přesun",
    moving: "Přesouvám…",
    couldntReschedule: "Nepodařilo se přesunout",
    statusCancelled: "Zrušeno",
    statusEnded: "Proběhlo",
    statusConfirmed: "Potvrzeno",

    dialogTitlePublicWith: (name) => `Rezervace — ${name}`,
    dialogTitlePublic: "Rezervace schůzky",
    dialogTitleAuthed: "Nová schůzka",
    pickLocation: "Vyberte místo",
    enterDifferentAddress: "Zadat jinou adresu",
    enterDifferentAddressSelect: "Zadat jinou adresu…",
    yourAddress: "Vaše adresa",
    whereLabel: "Kde",
    addressPlaceholder: "Adresa, kavárna, zasedačka…",
    durationLabel: "Délka",
    startLabel: "Začátek",
    noStartFits: "Žádný začátek se nevejde do zvolené délky. Zkuste kratší variantu.",
    onlyFitsHint: (duration) =>
      `Do tohoto volného bloku se vejde jen ${duration} — pro delší termín zkuste jiný den.`,
    yourName: "Vaše jméno",
    yourEmail: "Váš e-mail",
    titleLabel: "Název",
    notesLabel: "Poznámka",
    notesPlaceholderOnline: "Agenda, odkazy, kontext…",
    notesPlaceholderPhysical: "Co by měl organizátor vědět?",
    addNote: "+ Přidat poznámku",
    cancelAction: "Zrušit",
    bookAction: "Rezervovat",
    sendRequestAction: "Odeslat žádost",
    bookingInProgress: "Rezervuji…",
    sending: "Odesílám…",
    tileOnline: "Online",
    tileOnlineSub: "Rezervováno hned · odkaz na schůzku",
    tilePhysical: "Osobně",
    tilePhysicalSub: "Hostitel musí potvrdit",
    recommended: "Doporučeno",
    pickOneOption: "— vyberte —",
    requiredField: "povinné",

    viewDay: "Den",
    viewThreeDays: "3 dny",
    viewWeek: "Týden",
    previousDay: "Předchozí den",
    previousThreeDays: "Předchozí 3 dny",
    previousWeek: "Předchozí týden",
    nextDay: "Další den",
    nextThreeDays: "Další 3 dny",
    nextWeek: "Další týden",
    freeIntervalTooltip: (when) => `${when} volno`,
    todayLabel: "Dnes",
    weekLabel: (n) => `Týden ${n}`,
    slotTooltipBookable: (dur, from, to, latest) =>
      `Volno pro schůzku ${dur} min kdykoli mezi ${from} a ${to}.\nNejpozdější začátek: ${latest}.\nKlikněte pro rezervaci.`,
    slotTooltipCopyable: (dur, from, to, latest) =>
      `Volno pro schůzku ${dur} min kdykoli mezi ${from} a ${to}.\nNejpozdější začátek: ${latest}.\nKlikněte pro zkopírování.`,
  },
  en: {
    loading: "Loading…",
    getYourOwn: "Get your own",
    langSwitchAria: "Choose language",

    profileNotFound: "Profile not found",
    profileNotFoundHint: "This link is invalid or no longer public.",
    countryAndWindow: (country) => `${country} · Availability for the next 8 weeks`,
    notAcceptingBookings:
      "This user is not currently accepting bookings — contact them directly.",
    inPersonOnlyTitle: "This user only accepts in-person bookings.",
    inPersonOnlyHint: "Pick a slot to see available locations.",
    bookingsOffFor: (name) => `${name} isn't accepting direct bookings yet.`,
    bookingsOffHint:
      "You can still see their availability below — reach out another way to arrange a time.",
    noFreeTime: "No free time in the upcoming weeks.",
    pickAMeeting: "Pick a meeting",
    pickAMeetingHint: "Choose an option — the next step shows when we're both free.",
    backToMeetingTypes: "Back to meeting types",
    meetingLinkLabel: "Meeting link",
    meetingLinkAuto: "Auto",
    meetingLinkGoogleMeet: "Google Meet",
    meetingLinkTeams: "Microsoft Teams",
    bookingRequestSent: (name) =>
      `Request sent. ${name} will confirm or decline — you'll get an email either way.`,
    bookingConfirmed: "Booked! Invite is on its way to your inbox.",
    joinMeeting: "Join meeting",
    manageBooking: "Manage booking",

    loadingYourBooking: "Loading your booking…",
    bookingNotFoundTitle: "Booking not found",
    bookingNotFoundBody:
      "This link doesn't match any booking. It may have been mistyped or the booking was already deleted from the host's calendar.",
    couldntLoadBooking: "Couldn't load this booking. Try again in a minute.",
    meetingWith: (name) => `Meeting with ${name}`,
    bookingAttendeeWith: (name) => `with ${name}`,
    rowWhen: "When",
    rowWhere: "Where",
    rowBookedAs: "Booked as",
    bookingCancelledTemplate: (when) =>
      when
        ? `This booking is cancelled (${when}). The host has been notified.`
        : "This booking is cancelled. The host has been notified.",
    bookingAlreadyEnded: "This booking has already ended.",
    cancelBookingAction: "Cancel",
    rescheduleAction: "Reschedule",
    cancelConfirmTitle: "Cancel this booking?",
    cancelConfirmBody:
      "The calendar event will be removed and the host will be notified. This can't be undone.",
    cancelReasonLabel: "Reason (optional)",
    cancelReasonPlaceholder: "Let the host know if you'd like.",
    keepIt: "Keep it",
    yesCancel: "Yes, cancel",
    cancelling: "Cancelling…",
    couldntCancel: "Couldn't cancel",
    backToBooking: "Back to booking",
    pickNewTime: "Pick a new time",
    rescheduleHint: (name) =>
      `${name}'s next 8 weeks. Click any free slot to confirm the move — the old event will be removed and a new invite sent for the new time.`,
    couldntLoadAvailability: "Couldn't load host availability.",
    moveMeetingTitle: "Move meeting?",
    moveMeetingBody: (name) =>
      `Confirm the new time — ${name} will be notified and the calendar event updated for everyone.`,
    rowWas: "Was",
    rowNow: "Now",
    backBtn: "Back",
    confirmMove: "Confirm move",
    moving: "Moving…",
    couldntReschedule: "Couldn't reschedule",
    statusCancelled: "Cancelled",
    statusEnded: "Ended",
    statusConfirmed: "Confirmed",

    dialogTitlePublicWith: (name) => `Book with ${name}`,
    dialogTitlePublic: "Book a meeting",
    dialogTitleAuthed: "New meeting",
    pickLocation: "Pick a location",
    enterDifferentAddress: "Enter a different address",
    enterDifferentAddressSelect: "Enter a different address…",
    yourAddress: "Your address",
    whereLabel: "Where",
    addressPlaceholder: "Address, café, meeting room…",
    durationLabel: "Duration",
    startLabel: "Start",
    noStartFits: "No start time fits the picked duration. Try a shorter one.",
    onlyFitsHint: (duration) =>
      `Only ${duration} fits this free block — pick another day for a longer slot.`,
    yourName: "Your name",
    yourEmail: "Your email",
    titleLabel: "Title",
    notesLabel: "Notes",
    notesPlaceholderOnline: "Agenda, links, context…",
    notesPlaceholderPhysical: "Anything the host should know?",
    addNote: "+ Add a note",
    cancelAction: "Cancel",
    bookAction: "Book",
    sendRequestAction: "Send request",
    bookingInProgress: "Booking…",
    sending: "Sending…",
    tileOnline: "Online",
    tileOnlineSub: "Books now · meeting link",
    tilePhysical: "In person",
    tilePhysicalSub: "Needs host approval",
    recommended: "Recommended",
    pickOneOption: "— pick one —",
    requiredField: "required",

    viewDay: "Day",
    viewThreeDays: "3 days",
    viewWeek: "Week",
    previousDay: "Previous day",
    previousThreeDays: "Previous 3 days",
    previousWeek: "Previous week",
    nextDay: "Next day",
    nextThreeDays: "Next 3 days",
    nextWeek: "Next week",
    freeIntervalTooltip: (when) => `${when} free`,
    todayLabel: "Today",
    weekLabel: (n) => `Week ${n}`,
    slotTooltipBookable: (dur, from, to, latest) =>
      `Free for a ${dur}-min meeting any time between ${from} and ${to}.\nLatest possible start: ${latest}.\nClick to book.`,
    slotTooltipCopyable: (dur, from, to, latest) =>
      `Free for a ${dur}-min meeting any time between ${from} and ${to}.\nLatest possible start: ${latest}.\nClick to copy.`,
  },
};
