import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideIonicAngular } from '@ionic/angular';

import { SUBMISSIONS } from '../../testing/fixtures';
import { SubmissionsPage } from './submissions.page';

describe('SubmissionsPage', () => {
  it('lists suggestions with their review state', () => {
    TestBed.configureTestingModule({
      imports: [SubmissionsPage],
      providers: [provideIonicAngular(), provideHttpClient(), provideHttpClientTesting()],
    });
    const fixture = TestBed.createComponent(SubmissionsPage);
    TestBed.inject(HttpTestingController).expectOne('/api/v1/submissions').flush(SUBMISSIONS);
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('Outreach Data Audit');
    expect(fixture.nativeElement.textContent).toContain('In review');
    expect(fixture.nativeElement.textContent).toContain('Draft: not yet submitted');
  });

  it('shows the label the server sent, and says when the list cannot be loaded', () => {
    TestBed.configureTestingModule({
      imports: [SubmissionsPage],
      providers: [provideIonicAngular(), provideHttpClient(), provideHttpClientTesting()],
    });
    let fixture = TestBed.createComponent(SubmissionsPage);
    const http = TestBed.inject(HttpTestingController);
    http.expectOne('/api/v1/submissions').flush([{ ...SUBMISSIONS[0], state_label: 'Label from the server' }]);
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('Label from the server');
    fixture = TestBed.createComponent(SubmissionsPage);
    http.expectOne('/api/v1/submissions').flush('down', { status: 500, statusText: '' });
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('[role="alert"]').textContent).toContain('Could not load your suggestions');
  });
});
