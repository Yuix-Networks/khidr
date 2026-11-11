from django.urls import path

from . import views

urlpatterns = [
    path("", views.project_list, name="project_list"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),

    # projects
    path("projects/create/", views.create_project_view, name="create_project"),
    path("projects/<int:project_id>/", views.project_detail, name="project_detail"),

    # tasks
    path("tasks/create/", views.create_task, name="create_task"),
    path("tasks/<int:task_id>/", views.task_detail, name="task_detail"),
    path("tasks/<int:task_id>/edit/", views.edit_task, name="edit_task"),
    path("tasks/<int:task_id>/delete/", views.delete_task, name="delete_task"),
    path("tasks/<int:task_id>/send/", views.send_message, name="send_message"),
    path("tasks/<int:task_id>/approve/", views.approve_task, name="approve_task"),
    path("tasks/<int:task_id>/move_to_todo/", views.move_to_todo, name="move_to_todo"),
    path("tasks/<int:task_id>/request_changes/", views.request_changes, name="request_changes"),
    path("tasks/<int:task_id>/status/", views.set_task_status, name="set_task_status"),
    path("tasks/<int:task_id>/close_and_create/", views.close_and_create_task, name="close_and_create_task"),
]
