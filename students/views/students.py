import datetime

from django.core.exceptions import ObjectDoesNotExist, FieldDoesNotExist, FieldError
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.db import transaction
from django.db.models import Count
from django.core.urlresolvers import reverse_lazy
from django.utils.decorators import method_decorator
from django.views.generic.edit import CreateView, FormView, UpdateView
from django.views.generic.list import ListView
from django.views.generic.detail import DetailView
from django.contrib.auth.forms import UserCreationForm
from braces.views import LoginRequiredMixin
from django.contrib.auth import authenticate, login
from students.forms import CourseEnrollForm
from students.forms import StudentSignupForm
from students.forms import StudentInterestsForm
from students.forms import TakeQuizForm
from courses.models import Course
from students.models import Quiz
from students.models import Student
from students.models import TakenQuiz
from students.models import User
from courses.models import Review
from courses.models import Cluster
from django.core.mail import mail_admins
from django.contrib import messages
from students.decorators import student_required

from courses.suggestions import update_clusters

class StudentCourseListView(LoginRequiredMixin, ListView):
    model = Course
    template_name = 'students/course/list.html'

    def get_queryset(self):
        qs = super(StudentCourseListView, self).get_queryset()
        return qs.filter(students__in=[self.request.user])


class StudentCourseDetailView(LoginRequiredMixin, DetailView):
    model = Course
    template_name = 'students/course/detail.html'

    def get_queryset(self):
        qs = super(StudentCourseDetailView, self).get_queryset()
        return qs.filter(students__in=[self.request.user])

    def get_context_data(self, **kwargs):
        context = super(StudentCourseDetailView, self).get_context_data(**kwargs)
        course = self.get_object()
        if 'module_id' in self.kwargs:
            #get current module
            context['module'] = course.modules.get(id=self.kwargs['module_id'])
        else:
            #get first module
            context['module'] = course.modules.all()[0]
        return context


class StudentRegistrationView(CreateView):
    model = User
    template_name = 'registration/signup_form.html'
    form_class = StudentSignupForm
    success_url = reverse_lazy('student_course_list')

    def get_context_data(self, **kwargs):
        kwargs['user_type'] = 'student'
        return super().get_context_data(**kwargs)

    def form_valid(self, form):
        result = super(StudentRegistrationView, self).form_valid(form)
        cd = form.cleaned_data
        user = authenticate(username=cd['username'], password=cd['password1'])
        mail_admins("A new student user is sign up", "check email on myelearning")
        login(self.request, user)
        return result


class StudentEnrollCourseView(FormView):
    course = None
    form_class = CourseEnrollForm

    def form_valid(self, form):
        self.course = form.cleaned_data['course']
        self.course.students.add(self.request.user)
        return super(StudentEnrollCourseView, self).form_valid(form)

    def get_success_url(self):
        return reverse_lazy('student_course_detail', args=[self.course.id])


@method_decorator([login_required, student_required], name='dispatch')
class StudentInterestsView(UpdateView):
    model = Student
    form_class = StudentInterestsForm
    template_name = 'students/student/interests_form.html'
    success_url = reverse_lazy('student_quiz_list')

    def get_object(self):
        try:
            return self.request.user.student
        except ObjectDoesNotExist:
            return self.request.user


    def form_valid(self, form):
        messages.success(self.request, 'Interests updated with success.')
        return super().form_valid(form)


@method_decorator([login_required, student_required], name='dispatch')
class QuizListView(ListView):
    model = Quiz
    ordering = ('name',)
    context_object_name = 'quizzes'
    template_name = 'students/student/quiz_list.html'

    def get_queryset(self):
        try:
            student = self.request.user.student
            student_interests = student.interests.values_list('pk', flat=True)
            taken_quizzes = student.quizzes.values_list('pk', flat=True)
            queryset =  Quiz.objects.filter(tags__in=student_interests).exclude(pk__in=taken_quizzes).annotate(question_count=Count('questions')).filter(question_count__gt=0)
            return queryset
        except ObjectDoesNotExist:
            return self.request.user


@method_decorator([login_required, student_required], name='dispatch')
class TakenQuizListView(ListView):
    model = TakenQuiz
    context_object_name = 'taken_quizzes'
    template_name = 'students/student/taken_quiz_list.html'

    def get_queryset(self):
        queryset = self.request.user.student.taken_quizzes.select_related('quiz', 'quiz__tags').order_by('quiz__name')
        return queryset


@login_required
@student_required
def take_quiz(request, pk):
    quiz = get_object_or_404(Quiz, pk=pk)
    student = request.user.student

    if student.quizzes.filter(pk=pk).exists():
        return render(request, 'students/student/taken_quiz_list.html')

    total_questions = quiz.questions.count()
    unanswered_questions = student.get_unanswered_questions(quiz)
    total_unanswered_questions = unanswered_questions.count()
    progress = 100 - round(((total_unanswered_questions - 1) / total_questions) * 100)
    question = unanswered_questions.first()

    if request.method == 'POST':
        form = TakeQuizForm(question=question, data=request.POST)
        if form.is_valid():
            with transaction.atomic():
                student_answer = form.save(commit=False)
                student_answer.student = student
                student_answer.save()
                if student.get_unanswered_questions(quiz).exists():
                    return redirect('take_quiz', pk)
                else:
                    correct_answers = student.quiz_answers.filter(answer__question__quiz=quiz, answer__is_correct=True).count()
                    score = round((correct_answers / total_questions ) * 100.0, 2)
                    TakenQuiz.objects.create(student=student, quiz=quiz, score=score)
                    if score < 50.0:
                        messages.warning(request, 'Good luck for next time! Your score for this quiz %s was %s.' % (quiz.name, score))
                    else:
                        messages.success(request, 'Fantastic! You completed the quiz %s with success! Your scored %s points.' % (quiz.name, score))
                    return redirect('student_quiz_list')
    else:
        form = TakeQuizForm(question=question)

    return render(request, 'students/student/take_quiz_form.html', {
        'quiz': quiz,
        'question': question,
        'form': form,
        'progress': progress
    })


@login_required
def student_recommendation_list(request):
    user_reviews = Review.objects.filter(user_name=request.user).prefetch_related('course')
    user_review_course_ids = set(map(lambda x: x.course.id, user_reviews))

    try:
        user_cluster_name = User.objects.get(username=request.user.username).cluster_set.first().name
    except FieldDoesNotExist or FieldError or NameError or ValueError or ObjectDoesNotExist:
        messages.error(self.request, 'There are no recommendations courses for you. Sorry ;-)')
    except:
        update_clusters()
        user_cluster_name = User.objects.get(username=request.user.username).cluster_set.first().name


    user_cluster_other_members = Cluster.objects.get(name=user_cluster_name).users.exclude(username=request.user.username).all()
    others_members_usernames = set(map(lambda x: x, user_cluster_other_members))

    others_users_reviews = Review.objects.filter(user_name__in=others_members_usernames).exclude(course__id__in=user_review_course_ids)
    others_users_reviews_courses_ids = set(map(lambda x: x.course.id, others_users_reviews))

    course_list = sorted(
        list(Course.objects.filter(id__in=others_users_reviews_courses_ids)),
        key=lambda x: x.average_rating(),
        reverse=True
    )
    return render(request, 'students/reviews/student_recommendation_list.html', {'student': request.user.username, 'course_list': course_list})
