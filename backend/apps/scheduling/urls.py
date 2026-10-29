from django.urls import path

from .views import (
    BookingRequestDecideView,
    BookingRequestListView,
    CalendarAccountDetailView,
    CalendarAccountSetDefaultView,
    CalendarAccountsListView,
    GoogleAccountStatusView,
    HostBookingCancelView,
    HostBookingListView,
    HostBookingsIcsExportView,
    MeetingCreateView,
    MeetingTypeDetailView,
    MeetingTypeListView,
    MicrosoftAccountStatusView,
    MicrosoftOAuthStartView,
    MicrosoftWritableCalendarsView,
    OAuthStartView,
    PublicBookingManageView,
    PublicBookingRescheduleView,
    PublicMeetingCreateView,
    UserLocationDetailView,
    UserLocationListCreateView,
    UserLocationSetDefaultView,
    WritableCalendarsView,
    microsoft_oauth_callback,
    oauth_callback,
)

urlpatterns = [
    path("oauth/google/start", OAuthStartView.as_view(), name="google-oauth-start"),
    path("oauth/google/callback", oauth_callback, name="google-oauth-callback"),
    path("google-account", GoogleAccountStatusView.as_view(), name="google-account"),
    path(
        "google-account/writable-calendars",
        WritableCalendarsView.as_view(),
        name="google-account-writable-calendars",
    ),
    path("oauth/microsoft/start", MicrosoftOAuthStartView.as_view(), name="microsoft-oauth-start"),
    path("oauth/microsoft/callback", microsoft_oauth_callback, name="microsoft-oauth-callback"),
    path("microsoft-account", MicrosoftAccountStatusView.as_view(), name="microsoft-account"),
    path(
        "microsoft-account/writable-calendars",
        MicrosoftWritableCalendarsView.as_view(),
        name="microsoft-account-writable-calendars",
    ),
    # Multi-account REST endpoints (Task 3)
    path(
        "calendar-accounts",
        CalendarAccountsListView.as_view(),
        name="calendar-accounts-list",
    ),
    path(
        "calendar-accounts/<str:composite_id>",
        CalendarAccountDetailView.as_view(),
        name="calendar-accounts-detail",
    ),
    path(
        "calendar-accounts/<str:composite_id>/set-default",
        CalendarAccountSetDefaultView.as_view(),
        name="calendar-accounts-set-default",
    ),
    # G2 — user location CRUD
    path("me/locations", UserLocationListCreateView.as_view(), name="me-locations"),
    path("me/locations/<int:pk>", UserLocationDetailView.as_view(), name="me-location-detail"),
    path(
        "me/locations/<int:pk>/set-default",
        UserLocationSetDefaultView.as_view(),
        name="me-location-set-default",
    ),
    path("meeting-types", MeetingTypeListView.as_view(), name="meeting-types-list"),
    path(
        "meeting-types/<int:pk>",
        MeetingTypeDetailView.as_view(),
        name="meeting-types-detail",
    ),
    path("meetings", MeetingCreateView.as_view(), name="meetings-create"),
    path(
        "public/meetings/<uuid:token>",
        PublicMeetingCreateView.as_view(),
        name="public-meetings-create",
    ),
    # Single URL, verb-based dispatch: GET returns booking details;
    # POST (with optional {"reason": "..."}) cancels the booking.
    path(
        "public/bookings/<uuid:uuid_>",
        PublicBookingManageView.as_view(),
        name="public-booking-manage",
    ),
    path(
        "public/bookings/<uuid:uuid_>/reschedule",
        PublicBookingRescheduleView.as_view(),
        name="public-booking-reschedule",
    ),
    path("booking-requests", BookingRequestListView.as_view(), name="booking-requests-list"),
    path("host-bookings", HostBookingListView.as_view(), name="host-bookings-list"),
    path(
        "host-bookings/export.ics",
        HostBookingsIcsExportView.as_view(),
        name="host-bookings-export-ics",
    ),
    path(
        "host-bookings/<uuid:uuid_>/cancel",
        HostBookingCancelView.as_view(),
        name="host-bookings-cancel",
    ),
    path(
        "booking-requests/<int:pk>/decide",
        BookingRequestDecideView.as_view(),
        name="booking-requests-decide",
    ),
]
