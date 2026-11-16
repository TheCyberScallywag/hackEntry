import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideIonicAngular } from '@ionic/angular';

import { ME, PLACEMENTS } from '../../testing/fixtures';
import { OpportunitiesPage } from './opportunities.page';

function render() {
  TestBed.configureTestingModule({
    imports: [OpportunitiesPage],
    providers: [provideIonicAngular(), provideHttpClient(), provideHttpClientTesting()],
  });
  const fixture = TestBed.createComponent(OpportunitiesPage);
  const http = TestBed.inject(HttpTestingController);
  http.expectOne('/api/v1/placements').flush(PLACEMENTS);
  fixture.detectChanges();
  return { fixture, page: fixture.componentInstance, http };
}

describe('OpportunitiesPage', () => {
  it('lists eligible offers with yes/no and teams with leave', () => {
    const { fixture } = render();
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('Eligible: awaiting answers');
    expect(text).toContain("Yes, I'd like to help");
    expect(text).toContain('On the team');
    expect(text).toContain('Leave the team');
  });

  it('sends the answer with the version and reloads', () => {
    const { page, http } = render();
    page.answer(PLACEMENTS[0], 'yes');
    const req = http.expectOne('/api/v1/placements/p_2/answer');
    expect(req.request.body).toEqual({ answer: 'yes', version: 1 });
    req.flush({ state: 'team' });
    expect(page.message()).toBe('You are on the team!');
    http.expectOne('/api/v1/placements').flush([]);
    http.expectOne('/api/v1/me').flush(ME);
  });

  it('reloads when the offer changed', () => {
    const { page, http } = render();
    page.answer(PLACEMENTS[0], 'no');
    http.expectOne('/api/v1/placements/p_2/answer').flush({ detail: 'stale' }, { status: 409, statusText: '' });
    expect(page.message()).toContain('changed');
    http.expectOne('/api/v1/placements').flush(PLACEMENTS);
  });

  it('says so when the list cannot be loaded', () => {
    TestBed.configureTestingModule({
      imports: [OpportunitiesPage],
      providers: [provideIonicAngular(), provideHttpClient(), provideHttpClientTesting()],
    });
    const fixture = TestBed.createComponent(OpportunitiesPage);
    TestBed.inject(HttpTestingController).expectOne('/api/v1/placements').flush('down', { status: 503, statusText: '' });
    expect(fixture.componentInstance.message()).toBe('Could not load your opportunities.');
  });

  it('leaves a team, and reloads with a message when that fails', () => {
    const { page, http } = render();
    page.leave(PLACEMENTS[1]);
    http.expectOne('/api/v1/placements/p_4/leave').flush({ state: 'left' });
    expect(page.message()).toBe('You left the team.');
    http.expectOne('/api/v1/placements').flush(PLACEMENTS);
    page.leave(PLACEMENTS[1]);
    http.expectOne('/api/v1/placements/p_4/leave').flush({ detail: 'Not found' }, { status: 404, statusText: '' });
    expect(page.message()).toContain('Could not leave');
    http.expectOne('/api/v1/placements').flush(PLACEMENTS);
  });
});
