from django.urls import path

from qa import views

urlpatterns = [
    path("questions", views.create_question, name="qa-create-question"),
    path("jobs/<uuid:job_id>", views.get_job, name="qa-get-job"),
]
